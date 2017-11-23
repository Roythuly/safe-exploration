# -*- coding: utf-8 -*-
"""
Created on Mon Sep 25 14:54:53 2017

@author: tkoller

TODO: Untested (But reused)!!
TODO: Undocumented!

"""

from casadi import mtimes, exp, sum1, sum2, repmat, SX, jacobian, Function, sqrt, vertcat, horzcat
import numpy as np

def _k_rbf(x,hyp,y = None,diag_only = False):
    """ Evaluate the RBF kernel function symbolically using Casadi
    
    """
    lenscales = hyp["rbf_lengthscales"]
    precision = hyp["rbf_variance"]
    T,_ = x.shape
    
    if diag_only:
        ret = SX(T,)
        ret[:] = precision
        return ret
    
    n_x,_ = np.shape(x)
    
    if y is None:
        y = x
    n_y,_ = np.shape(y)
    
    lens_x = repmat(lenscales.reshape(1,-1),n_x)
    lens_y = repmat(lenscales.reshape(1,-1),n_y)
    r = _unscaled_dist(x / lens_x,y / lens_y)
        
    return precision * exp(-0.5 * r**2)

    
def _k_prod_lin_rbf(x,hyp,y = None,diag_only = False):
    """ Evaluate the prdocut of linear and rbf kernel function symbolically using Casadi
    
    """
    lin_variances = hyp["lin_variances"]
    rbf_lengthscales = hyp["rbf_lengthscales"]
    rbf_variance = hyp["rbf_variance"]
    
    return _k_rbf(x,hyp,y,diag_only)*_k_lin(x,hyp,y,diag_only)
        
    
def _k_lin(x,hyp,y = None,diag_only = False):
    """ Evaluate the Linear kernel function symbolically using Casadi
    
    """
    var = hyp["lin_variances"]
    n_x, _ = np.shape(x)
    var_x = sqrt(repmat(var.reshape(1,-1),n_x))
    if y is None:
        var_y = var_x
        y = x
    else:
        n_y, _ = np.shape(y)
        var_y = sqrt(repmat(var.reshape(1,-1),n_y))
    
    return mtimes(x*var_x,(y*var_y).T)
    
        
def _unscaled_dist(x,y):
    """ calculate the squared distance between two sets of datapoints
    
    
    
    Source:
    https://github.com/SheffieldML/GPy/blob/devel/GPy/kern/src/stationary.py
    """
    n_x,_ = np.shape(x)
    n_y,_ = np.shape(y)
    x1sq = sum2(x**2)
    x2sq = sum2(y**2)
    r2 = -2 * mtimes(x,y.T) + repmat(x1sq,1,n_y) + repmat(x2sq.T,n_x,1)
    
    return sqrt(r2)
  
  
def gp_pred(x,kern,beta,x_train,hyp,k_inv_training = None,pred_var = True):
    """
    
    """
    n_pred, _ = np.shape(x)

    k_star = kern(x,hyp,y = x_train)
    pred_mu = mtimes(k_star,beta)
    
    if pred_var:
        if k_inv_training is None:
                raise ValueError("""The inverted kernel matrix is required 
                for computing the predictive variance""") 
                    
        k_expl_var = kern(x,hyp,diag_only = True)
        pred_sigm = k_expl_var- sum2(mtimes(k_star,k_inv_training)*k_star)
            
        return pred_mu, pred_sigm
        
    return pred_mu
    
    
def _get_kernel_function(kern_type):
    """ Return the casadi function for a specific kernel type
    
    Parameters
    ----------
    kern_type: str
        The identifier of the kernel
    
    Returns
    -------
        f_pred: function 
            The python function containing the casadi code representing
            the given kern_type
    """
    if kern_type == "rbf":
        return _k_rbf
    elif kern_type == "prod_lin_rbf":
        return _k_prod_lin_rbf
    else:
        raise ValueError("Unknown kernel type")
    
               
def gp_pred_function(x,x_train,beta,hyp,kern_types,k_inv_training = None, pred_var = True, compute_grads = False):
    """
    
    """    
    n_gps = np.shape(beta)[1]
    inp = SX.sym("input",(x.shape))
    out_dict = dict()
    mu_all = []
    pred_sigma_all = []
    jac_mu_all = []
    for i in range(n_gps):
        kern_i = _get_kernel_function(kern_types[i])
        beta_i = beta[:,i]
        hyp_i = hyp[i]
        k_inv_i = None
        if not k_inv_training is None:
            k_inv_i = k_inv_training[i]
         
        if pred_var:
            mu_new,  sigma_new = gp_pred(inp,kern_i,beta_i,x_train,hyp_i,k_inv_i,pred_var)
            pred_func = Function("pred_func",[inp],[mu_new,sigma_new],["inp"],["mu_1","sigma_1"])
            F_1 = pred_func(inp=x)
            pred_sigma = F_1["sigma_1"]
            pred_sigma_all = horzcat(pred_sigma,pred_sigma_all)
        else: 
            mu_new = gp_pred(inp,kern_i,beta_i,x_train,hyp_i,k_inv_i,pred_var)  
            pred_func = Function("pred_func",[inp],[mu_new],["inp"],["mu_1"])
            F_1 = pred_func(inp=x)
        
        mu_1 = F_1["mu_1"]
        mu_all = horzcat(mu_all,mu_1)
        
        if compute_grads:
            jac_func = pred_func.jacobian("inp","mu_1")
            F_1_jac = jac_func(inp = x)
            jac_mu = F_1_jac["dmu_1_dinp"]
            jac_mu_all = vertcat(jac_mu_all,jac_mu)
            
            
    out_dict["pred_mu"] = mu_all    
    if pred_var:
        out_dict["pred_sigma"] = pred_sigma_all
    if compute_grads:
        out_dict["jac_mu"] = jac_mu_all
            
    
    return out_dict
    
        
    
            
        
        
    