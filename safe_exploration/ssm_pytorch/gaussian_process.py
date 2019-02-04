"""Gaussian process utlilities for gpytorch."""


import torch
from torch.nn import ModuleList
import gpytorch
from gpytorch.distributions import MultivariateNormal
from safe_exploration.state_space_models import StateSpaceModel
from hessian import compute_hessian
from .utilities import compute_jacobian

__all__ = ['BatchMean', 'BatchKernel', 'LinearMean', 'MultiOutputGP']


class BatchMean(gpytorch.means.Mean):
    """Combine different mean functions across batches.

    Parameters
    ----------
    base_means : list
        List of mean functions used for each batch.
    """

    def __init__(self, base_means):
        super(BatchMean, self).__init__()

        self.base_means = ModuleList(base_means)

    @property
    def batch_size(self):
        """Return the batch_size of the underlying model."""
        return len(self.base_kernels)

    def __getitem__(self, item):
        """Retrieve the ith mean."""
        return self.base_means[item]

    def __iter__(self):
        """Iterate over the means."""
        yield from self.base_means

    def forward(self, input):
        """Evaluate the mean functions and combine to a `b x len(input[0])` matrix."""
        return torch.stack([mean(x) for x, mean in zip(input, self.base_means)])


class BatchKernel(gpytorch.kernels.Kernel):
    """Combine different covariance functions across batches.

    Parameters
    ----------
    base_kernels : list
        List of base kernels used for each batch.
    """

    def __init__(self, base_kernels):
        super(BatchKernel, self).__init__(batch_size=len(base_kernels))
        self.base_kernels = ModuleList(base_kernels)

    def __getitem__(self, item):
        """Retrieve the ith kernel."""
        return self.base_kernels[item]

    def __iter__(self):
        """Iterate over the kernels."""
        yield from self.base_kernels

    def forward(self, x1, x2, diag=False, batch_dims=None, **params):
        """Evaluate the kernel functions and combine them."""
        kernels = [kernel(x1[i], x2[i], **params)
                   for i, kernel in enumerate(self.base_kernels)]
        if diag:
            kernels = [kernel.diag() for kernel in kernels]
        else:
            kernels = [kernel.evaluate() for kernel in kernels]

        return torch.stack(kernels)

    def size(self, x1, x2):
        """Return the size of the resulting covariance matrix."""
        non_batch_size = (x1.size(-2), x2.size(-2))
        return torch.Size((x1.size(0),) + non_batch_size)


class LinearMean(gpytorch.means.Mean):
    """A linear mean function.

    If the matrix has more than one rows, the mean will be applied in batch-mode.

    Parameters
    ----------
    matrix : torch.tensor
        A 2d matrix. For each feature vector x in (d, 1) the output is `A @ x`.
    trainable : bool, optional
        Whether the mean matrix should be trainable as a parameter.
    prior : optional
        The gpytorch prior for the parameter. Ignored if trainable is False.
    """

    def __init__(self, matrix, trainable=False, prior=None):
        super().__init__()
        if trainable:
            self.register_parameter(name='matrix',
                                    parameter=torch.nn.Parameter(matrix))
            if prior is not None:
                self.register_prior('matrix_prior', prior, 'matrix')
        else:
            self.matrix = matrix

    @property
    def batch_size(self):
        return self.matrix.size(0)

    def forward(self, x):
        """Compute the linear product."""
        return torch.einsum('ij,ilj->il', self.matrix, x)


class WrappedNormal(object):
    """A wrapper around gpytorch.NormalDistribution that doesn't squeeze empty dims."""
    def __init__(self, normal):
        super().__init__()
        self.normal = normal

    def __getattr__(self, key):
        """Unsqueeze empty dimensions."""
        res = getattr(self.normal, key)
        batch_shape = self.normal.batch_shape
        if not batch_shape and key in ('mean', 'variance', 'covariance_matrix'):
            res = res.unsqueeze(0)

        return res


class MultiOutputGP(gpytorch.models.ExactGP):
    """A GP model that uses the gpytorch batch mode for multi-output predictions.

    The main difference to simple batch mode, is that the model assumes that all GPs
    use the same input data. Moreover, even for single-input data it outputs predictions
    together with a singular dimension for the batchsize.

    Parameters
    ----------
    train_x : torch.tensor
        A (n x d) tensor with n data points of d dimensions each.
    train_y : torch.tensor
        A (n x o) tensor with n data points across o output dimensions.
    kernel : gpytorch.kernels.Kernel
        A kernel with appropriate batchsize. See `BatchKernel`.
    likelihood : gpytorch.likelihoods.Likelihood
        A GP likelihood with appropriate batchsize.
    mean : gpytorch.means.Mean, optional
        The mean function with appropriate batchsize. See `BatchMean`. Defaults to
        `gpytorch.means.ZeroMean()`.
    """

    def __init__(self, train_x, train_y, kernel, likelihood, mean=None):
        if mean is None:
            mean = gpytorch.means.ZeroMean()

        if train_y.dim() > 1:
            # Try to remove the first data row if it's empty
            train_y = train_y.squeeze(0)

        if train_y.dim() > 1:
            train_x = train_x.expand(len(train_y), *train_x.shape)

        super(MultiOutputGP, self).__init__(train_x, train_y, likelihood)

        self.mean = mean
        self.kernel = kernel

    @property
    def batch_size(self):
        """Return the batch size of the model."""
        return self.kernel.batch_size

    def set_train_data(self, inputs=None, targets=None, strict=True):
        """Set the GP training data."""
        raise NotImplementedError('TODO')

    def loss(self, mml):
        """Return the negative log-likelihood of the model.

        Parameters
        ----------
        mml : marginal log likelihood
        """
        output = super().__call__(*self.train_inputs)
        return -mml(output, self.train_targets).sum()

    def __call__(self, *args, **kwargs):
        """Evaluate the underlying batch_mode model."""
        if self.batch_size > 1:
            args = [arg.unsqueeze(-1) if arg.ndimension() == 1 else arg for arg in args]
            # Expand input arguments across batches
            args = list(map(lambda x: x.expand(self.batch_size, *x.shape), args))
        normal = super().__call__(*args, **kwargs)

        if self.batch_size > 1:
            return normal
        else:
            return WrappedNormal(normal)

    def forward(self, x):
        """Compute the resulting batch-distribution."""
        return MultivariateNormal(self.mean(x), self.kernel(x))


def GPytorchSSM(StateSpaceModel):
    """ A Gaussian process state space model based on GPytorch.

    We approximate the function x_{t+1} = f(x_t, u_t) with x in (1 x n) and u in (1 x m)
    based on noisy observations of f.

    """
    def __init__(self, num_states, num_actions, train_x, train_y, kernel, likelihood, mean=None):
        """ """

        ## check compatability of the parameters required for super classes
        assert np.shape(train_x)[1] == num_states + num_actions, "Input needs to have dimensions N x(n + m)"
        assert np.shape(train_y)[1] == num_states, "Input needs to have dimensions N x n"

        self.pytorch_gp = MultiOutputGP(train_x,train_y,kernel,likelihood,mean)
        StateSpaceModel.__init__(self,num_states,num_actions)

    def _compute_hessian_mean(self, states, actions):
        """ Generate the hessian of the mean prediction

        Parameters
        ----------
        states : np.ndarray
            A (1 x n) array of states.
        actions : np.ndarray
            A (1 x m) array of actions.

        Returns
        -------
        hess_mean:

        """
        inp = torch.stack((torch.from_numpy(states),torch.from_numpy(actions)),dim=1)
        n_in = self.num_states+self.num_actions
        hess_mean = torch.empty(self.num_states,n_in,n_in)
        for i in range(self.num_states):
            hess_mean[i,:,:] = hessian.hessian(self.pytorch_gp.mean(inp)[0,i],inp)

        return hess_mean.numpy()

    def predict(self, states, actions, jacobians = False, full_cov = False):
        """Predict the next states and uncertainty.

        Parameters
        ----------
        states : np.ndarray
            A (N x n) array of states.
        actions : np.ndarray
            A (N x m) array of actions.
        jacobians : bool, optional
            If true, return two additional outputs corresponding to the jacobians.
        full_cov : bool, optional
            Whether to return the full covariance.

        Returns
        -------
        mean : np.ndarray
            A (N x n) mean prediction for the next states.
        variance : np.ndarray
            A (N x n) variance prediction for the next states. If full_cov is True,
            then instead returns the (n x N x N) covariance matrix for each independent
            output of the GP model.
        jacobian_mean : np.ndarray
            A (N x n x n + m) array with the jacobians for each datapoint on the axes.
        jacobian_variance : np.ndarray
            Only supported without the full_cov flag.
        """
        inp = torch.stack((torch.from_numpy(states),torch.from_numpy(actions)),dim=1)
        pred_mean = self.pytorch_gp.mean(inp).numpy()
        pred_var = self.pytorch_gp.kernel(inp).numpy()

        if jacobians:
            jac_mean = compute_jacobian(self.pytorch_gp.mean, inp)
            jac_var = compute_jacobian(self.pytorch_gp.kernel, inp)

            return pred_mean, pred_var, jac_mean, jac_var

        else:
            pred_mean, pred_var

    def linearize_predict(self, states, actions, jacobians=False, full_cov=False):
        """Predict the next states and uncertainty.

        Parameters
        ----------
        states : np.ndarray
            A (N x n) array of states.
        actions : np.ndarray
            A (N x m) array of actions.
        jacobians : bool, optional
            If true, return two additional outputs corresponding to the jacobians of the predictive
            mean, the linearized predictive mean and variance.
        full_cov : bool, optional
            Whether to return the full covariance.

        Returns
        -------
        mean : np.ndarray
            A (N x n) mean prediction for the next states.
        variance : np.ndarray
            A (N x n) variance prediction for the next states. If full_cov is True,
            then instead returns the (n x N x N) covariance matrix for each independent
            output of the GP model.
        jacobian_mean : np.ndarray
            A (N x n x (n + m) array with the jacobians for each datapoint on the axes.
        jacobian_variance : np.ndarray
            Only supported without the full_cov flag.
        hessian_mean: np.ndarray
            A (N x n*(n+m) x (n+m)) Array with the derivatives of each entry in the jacobian for each input
        """
        N , n = np.shape(state)

        if jacobians and n > 1:
            raise NotImplementedError("""'linearize_predict' currently only allows for single
                                          inputs, i.e. (1 x n) arrays, when computing jacobians.""")


        out = self.predict(states,actions,jacobians,False)

        if jacobians:
            hess_mean = self._compute_hessian_mean(states,actions)

            return out[0], out[1], out[2], out[3], hess_mean

        else:
            return out[0], out[1]
