# -*- coding: utf-8 -*-
"""
cyipopt: Python wrapper for the Ipopt optimization package, written in Cython.

Copyright (C) 2012-2015 Amit Aides
Copyright (C) 2015-2017 Matthias Kümmerer
Copyright (C) 2017-2023 cyipopt developers

License: EPL 2.0
"""

import sys

import numpy as np
try:
    import scipy
except ImportError:  # scipy is not installed
    SCIPY_INSTALLED = False
else:
    SCIPY_INSTALLED = True
    del scipy
    from scipy.optimize import approx_fprime
    import scipy.sparse
    try:
        from scipy.optimize import OptimizeResult
    except ImportError:
        # in scipy 0.14 Result was renamed to OptimizeResult
        from scipy.optimize import Result
        OptimizeResult = Result
    try:
        # MemoizeJac has been made a private class, see
        # https://github.com/scipy/scipy/issues/17572
        from scipy.optimize._optimize import MemoizeJac
    except ImportError:
        from scipy.optimize.optimize import MemoizeJac
    try:
        from scipy.sparse import coo_array
    except ImportError:
        # coo_array was introduced with scipy 1.8
        from scipy.sparse import coo_matrix as coo_array

import cyipopt


class IpoptProblemWrapper(object):
    """Class used to map a scipy minimize definition to a cyipopt problem.

    Parameters
    ==========
    fun : callable
        The objective function to be minimized: ``fun(x, *args, **kwargs) ->
        float``.
    args : tuple, optional
        Extra arguments passed to the objective function and its derivatives
        (``fun``, ``jac``, ``hess``).
    kwargs : dictionary, optional
        Extra keyword arguments passed to the objective function and its
        derivatives (``fun``, ``jac``, ``hess``).
    jac : callable, optional
        The Jacobian of the objective function: ``jac(x, *args, **kwargs) ->
        ndarray, shape(n, )``. If ``None``, SciPy's ``approx_fprime`` is used.
    hess : callable, optional
        If ``None``, the Hessian is computed using IPOPT's numerical methods.
        Explicitly defined Hessians are not yet supported for this class.
    hessp : callable, optional
        If ``None``, the Hessian is computed using IPOPT's numerical methods.
        Explicitly defined Hessians are not yet supported for this class.
    constraints : {Constraint, dict} or List of {Constraint, dict}, optional
        See https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.minimize.html
        for more information. Note that the jacobian of each constraint
        corresponds to the `'jac'` key and must be a callable function
        with signature ``jac(x) -> {ndarray, coo_array}``. If the constraint's
        value of `'jac'` is a boolean and True, the constraint function `fun`
        is expected to return a tuple `(con_val, con_jac)` consisting of the
        evaluated constraint `con_val` and the evaluated jacobian `con_jac`.
    eps : float, optional
        Epsilon used in finite differences.
    con_dims : array_like, optional
        Dimensions p_1, ..., p_m of the m constraint functions
        g_1, ..., g_m : R^n -> R^(p_i).
    sparse_jacs: array_like, optional
        If sparse_jacs[i] = True, the i-th constraint's jacobian is sparse.
        Otherwise, the i-th constraint jacobian is assumed to be dense.
    jac_nnz_row: array_like, optional
        The row indices of the nonzero elements in the stacked
        constraint jacobian matrix
    jac_nnz_col: array_like, optional
        The column indices of the nonzero elements in the stacked
        constraint jacobian matrix
    """

    def __init__(self,
                 fun,
                 args=(),
                 kwargs=None,
                 jac=None,
                 hess=None,
                 hessp=None,
                 constraints=(),
                 eps=1e-8,
                 con_dims=(),
                 sparse_jacs=(),
                 jac_nnz_row=(),
                 jac_nnz_col=()):
        if not SCIPY_INSTALLED:
            msg = 'Install SciPy to use the `IpoptProblemWrapper` class.'
            raise ImportError()
        self.obj_hess = None
        self.last_x = None
        if hessp is not None:
            msg = 'Using hessian matrix times an arbitrary vector is not yet implemented!'
            raise NotImplementedError(msg)
        if hess is not None:
            self.obj_hess = hess
        if jac is None:
            jac = lambda x0, *args, **kwargs: approx_fprime(
                x0, fun, eps, *args, **kwargs)
        elif jac is True:
            fun = MemoizeJac(fun)
            jac = fun.derivative
        elif not callable(jac):
            raise NotImplementedError('jac has to be bool or a function')
        self.fun = fun
        self.jac = jac
        self.args = args
        self.kwargs = kwargs or {}
        self._constraint_funs = []
        self._constraint_jacs = []
        self._constraint_hessians = []
        self._constraint_dims = np.asarray(con_dims)
        self._constraint_args = []
        self._constraint_kwargs = []
        self._constraint_jac_is_sparse = sparse_jacs
        self._constraint_jacobian_structure = (jac_nnz_row, jac_nnz_col)
        if isinstance(constraints, dict):
            constraints = (constraints, )
        for con in constraints:
            con_fun = con['fun']
            con_jac = con.get('jac', None)
            con_args = con.get('args', [])
            con_hessian = con.get('hess', None)
            con_kwargs = con.get('kwargs', {})
            if con_jac is None:
                con_jac = lambda x0, *args, **kwargs: approx_fprime(
                    x0, con_fun, eps, *args, **kwargs)
            elif con_jac is True:
                con_fun = MemoizeJac(con_fun)
                con_jac = con_fun.derivative
            elif not callable(con_jac):
                raise NotImplementedError('jac has to be bool or a function')
            if (self.obj_hess is not None
                    and con_hessian is None) or (self.obj_hess is None
                                                 and con_hessian is not None):
                msg = "hessian has to be provided for the objective and all constraints"
                raise NotImplementedError(msg)
            self._constraint_funs.append(con_fun)
            self._constraint_jacs.append(con_jac)
            self._constraint_hessians.append(con_hessian)
            self._constraint_args.append(con_args)
            self._constraint_kwargs.append(con_kwargs)
        # Set up evaluation counts
        self.nfev = 0
        self.njev = 0
        self.nit = 0

    def evaluate_fun_with_grad(self, x):
        """ For backwards compatibility. """
        return (self.objective(x), self.gradient(x, **self.kwargs))

    def objective(self, x):
        self.nfev += 1
        return self.fun(x, *self.args, **self.kwargs)

    def gradient(self, x, **kwargs):
        self.njev += 1
        return self.jac(x, *self.args, **self.kwargs)  # .T

    def constraints(self, x):
        con_values = []
        for fun, args in zip(self._constraint_funs, self._constraint_args):
            con_values.append(fun(x, *args))
        return np.hstack(con_values)

    def jacobianstructure(self):
        return self._constraint_jacobian_structure

    def jacobian(self, x):
        # Convert all dense constraint jacobians to sparse ones.
        # The structure ( = row and column indices) is already known at this point,
        # so we only need to stack the evaluated jacobians
        jac_values = []
        for i, (jac, args) in enumerate(zip(self._constraint_jacs, self._constraint_args)):
            if self._constraint_jac_is_sparse[i]:
                jac_val = jac(x, *args)
                jac_values.append(jac_val.data)
            else:
                dense_jac_val = np.atleast_2d(jac(x, *args))
                jac_values.append(dense_jac_val.ravel())
        return np.hstack(jac_values)

    def hessian(self, x, lagrange, obj_factor):
        H = obj_factor * self.obj_hess(x)  # type: ignore
        # split the lagrangian multipliers for each constraint hessian
        lagrs = np.split(lagrange, np.cumsum(self._constraint_dims[:-1]))
        for hessian, args, lagr in zip(self._constraint_hessians,
                                       self._constraint_args, lagrs):
            H += hessian(x, lagr, *args)
        return H[np.tril_indices(x.size)]

    def intermediate(self, alg_mod, iter_count, obj_value, inf_pr, inf_du, mu,
                     d_norm, regularization_size, alpha_du, alpha_pr,
                     ls_trials):

        self.nit = iter_count


def get_bounds(bounds):
    if bounds is None:
        return None, None
    else:
        lb = [b[0] for b in bounds]
        ub = [b[1] for b in bounds]
        return lb, ub


def _get_sparse_jacobian_structure(constraints, x0):
    con_jac_is_sparse = []
    jacobians = []
    x0 = np.asarray(x0)
    if isinstance(constraints, dict):
        constraints = (constraints, )
    if len(constraints) == 0:
        return [], [], []
    for con in constraints:
        con_jac = con.get('jac', False)
        if con_jac:
            if isinstance(con_jac, bool):
                _, jac_val = con['fun'](x0, *con.get('args', []))
            else:
                jac_val = con_jac(x0, *con.get('args', []))
            # check if dense or sparse
            if isinstance(jac_val, coo_array):
                jacobians.append(jac_val)
                con_jac_is_sparse.append(True)
            else:
                # Creating the coo_array from jac_val would yield to
                # wrong dimensions if some values in jac_val are zero,
                # so we assume all values in jac_val are nonzero
                jacobians.append(coo_array(np.ones_like(np.atleast_2d(jac_val))))
                con_jac_is_sparse.append(False)
        else:
            # we approximate this jacobian later (=dense)
            con_val = np.atleast_1d(con['fun'](x0, *con.get('args', [])))
            jacobians.append(coo_array(np.ones((con_val.size, x0.size))))
            con_jac_is_sparse.append(False)
    J = scipy.sparse.vstack(jacobians)
    return con_jac_is_sparse, J.row, J.col


def get_constraint_dimensions(constraints, x0):
    con_dims = []
    if isinstance(constraints, dict):
        constraints = (constraints, )
    for con in constraints:
        if con.get('jac', False) is True:
            m = len(np.atleast_1d(con['fun'](x0, *con.get('args', []))[0]))
        else:
            m = len(np.atleast_1d(con['fun'](x0, *con.get('args', []))))
        con_dims.append(m)
    return np.array(con_dims)


def get_constraint_bounds(constraints, x0, INF=1e19):
    cl = []
    cu = []
    if isinstance(constraints, dict):
        constraints = (constraints, )
    for con in constraints:
        if con.get('jac', False) is True:
            m = len(np.atleast_1d(con['fun'](x0, *con.get('args', []))[0]))
        else:
            m = len(np.atleast_1d(con['fun'](x0, *con.get('args', []))))
        cl.extend(np.zeros(m))
        if con['type'] == 'eq':
            cu.extend(np.zeros(m))
        elif con['type'] == 'ineq':
            cu.extend(INF * np.ones(m))
        else:
            raise ValueError(con['type'])
    cl = np.array(cl)
    cu = np.array(cu)

    return cl, cu


def replace_option(options, oldname, newname):
    if oldname in options:
        if newname not in options:
            options[newname] = options.pop(oldname)


def convert_to_bytes(options):
    if sys.version_info >= (3, 0):
        for key in list(options.keys()):
            try:
                if bytes(key, 'utf-8') != key:
                    options[bytes(key, 'utf-8')] = options[key]
                    options.pop(key)
            except TypeError:
                pass


def minimize_ipopt(fun,
                   x0,
                   args=(),
                   kwargs=None,
                   method=None,
                   jac=None,
                   hess=None,
                   hessp=None,
                   bounds=None,
                   constraints=(),
                   tol=None,
                   callback=None,
                   options=None):
    """
    Minimize a function using ipopt. The call signature is exactly like for
    ``scipy.optimize.mimize``. In options, all options are directly passed to
    ipopt. Check [http://www.coin-or.org/Ipopt/documentation/node39.html] for
    details. The options ``disp`` and ``maxiter`` are automatically mapped to
    their ipopt-equivalents ``print_level`` and ``max_iter``.
    """
    if not SCIPY_INSTALLED:
        msg = 'Install SciPy to use the `minimize_ipopt` function.'
        raise ImportError(msg)

    _x0 = np.atleast_1d(x0)

    lb, ub = get_bounds(bounds)
    cl, cu = get_constraint_bounds(constraints, _x0)
    con_dims = get_constraint_dimensions(constraints, _x0)
    sparse_jacs, jac_nnz_row, jac_nnz_col = _get_sparse_jacobian_structure(
        constraints, _x0)

    problem = IpoptProblemWrapper(fun,
                                  args=args,
                                  kwargs=kwargs,
                                  jac=jac,
                                  hess=hess,
                                  hessp=hessp,
                                  constraints=constraints,
                                  eps=1e-8,
                                  con_dims=con_dims,
                                  sparse_jacs=sparse_jacs,
                                  jac_nnz_row=jac_nnz_row,
                                  jac_nnz_col=jac_nnz_col)

    if options is None:
        options = {}

    nlp = cyipopt.Problem(n=len(_x0),
                          m=len(cl),
                          problem_obj=problem,
                          lb=lb,
                          ub=ub,
                          cl=cl,
                          cu=cu)

    # python3 compatibility
    convert_to_bytes(options)

    # Rename some default scipy options
    replace_option(options, b'disp', b'print_level')
    replace_option(options, b'maxiter', b'max_iter')
    if b'print_level' not in options:
        options[b'print_level'] = 0
    if b'tol' not in options:
        options[b'tol'] = tol or 1e-8
    if b'mu_strategy' not in options:
        options[b'mu_strategy'] = b'adaptive'
    if b'hessian_approximation' not in options:
        if hess is None and hessp is None:
            options[b'hessian_approximation'] = b'limited-memory'
    for option, value in options.items():
        try:
            nlp.add_option(option, value)
        except TypeError as e:
            msg = 'Invalid option for IPOPT: {0}: {1} (Original message: "{2}")'
            raise TypeError(msg.format(option, value, e))

    x, info = nlp.solve(_x0)

    if np.asarray(x0).shape == ():
        x = x[0]

    return OptimizeResult(x=x,
                          success=info['status'] == 0,
                          status=info['status'],
                          message=info['status_msg'],
                          fun=info['obj_val'],
                          info=info,
                          nfev=problem.nfev,
                          njev=problem.njev,
                          nit=problem.nit)
