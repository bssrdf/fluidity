# Copyright (C) 2011 Anders Logg
# Copyright (C) 2012 Graham Markall, Florian Rathgeber
#
# This file is part of DOLFIN.
#
# DOLFIN is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# DOLFIN is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with DOLFIN. If not, see <http://www.gnu.org/licenses/>.

__all__ = ["LinearVariationalProblem",
           "LinearVariationalSolver",
           "NonlinearVariationalProblem",
           "NonlinearVariationalSolver",
           "solve"]

import numpy

import ufl
from ufl.finiteelement import FiniteElement, VectorElement, TensorElement
from bcs import BoundaryCondition, DirichletBC
from pyop2 import op2, ffc_interface
from libspud import get_option, have_option, SpudKeyError

class LinearVariationalProblem(object):

    def __init__(self, a, L, u, bcs=None,
                 form_compiler_parameters=None):
        """
        Create linear variational problem a(u, v) = L(v).

        An optional argument bcs may be passed to specify boundary
        conditions.

        Another optional argument form_compiler_parameters may be
        specified to pass parameters to the form compiler.
        """

        # Extract and check arguments
        u = _extract_u(u)
        self.bcs = _extract_bcs(bcs)

        # Check if linear form L is empty
        if L.integrals() == ():
            L = v*0*dx

        # Store input UFL forms and solution Function
        self.a = a
        self.L = L
        self.u = u

        # Store form compiler parameters
        form_compiler_parameters = form_compiler_parameters or {}
        self.form_compiler_parameters = form_compiler_parameters

class NonlinearVariationalProblem(object):

    def __init__(self, F, u, bcs=None, J=None,
                 form_compiler_parameters=None):
        """
        Create nonlinear variational problem F(u; v) = 0.

        Optional arguments bcs and J may be passed to specify boundary
        conditions and the Jacobian J = dF/du.

        Another optional argument form_compiler_parameters may be
        specified to pass parameters to the form compiler.
        """

        # Extract and check arguments
        u = _extract_u(u)
        bcs = _extract_bcs(bcs)

        # Store input UFL forms and solution Function
        self.F_ufl = F
        self.J_ufl = J
        self.u_ufl = u

        # Store form compiler parameters
        form_compiler_parameters = form_compiler_parameters or {}
        self.form_compiler_parameters = form_compiler_parameters

_apply_dirichlet_1 = """\
void apply_dirichlet(double **t, double *val) { t[0][0] = *val; t[1][0] = *val; }
"""

_apply_dirichlet = {}

class LinearVariationalSolver(object):
    """Solves a linear variational problem."""
    
    def __init__(self, problem):
        _apply_dirichlet[1] = op2.Kernel(_apply_dirichlet_1, "apply_dirichlet")
        self._problem = problem
        #FIXME: Make source-compatible with Dolfin parameters interface
        self.parameters = {}

    def solve(self):
        A = _assemble_tensor(self._problem.a)
        b = _assemble_tensor(self._problem.L)

        mesh = self._problem.u.mesh
        mesh.compute_boundaries()

        # Apply Dirichlet BCs
        for bc in self._problem.bcs:
            if not isinstance(bc, DirichletBC):
                raise NotImplementedError("Only DirichletBCs supported")
            for domain in bc.domain_args:
                # Get list of nodes in domain.
                # FIXME: get zero_rows to take a map
                # so we don't mandate pulling data back and forth in
                # the interface.
                domain_nodes = set()
                if domain not in mesh.faces.boundary_list:
                    iterset = op2.Set(0)
                    elem_map = op2.Map(iterset, mesh.node_set,
                                       mesh.faces.surface_mesh.shape.loc,
                                       numpy.zeros(0))
                else:
                    iterset = mesh.faces.surface_elem_sets[domain]
                    elem_map = mesh.faces.surface_elements_to_nodes_maps[domain]
                    for nodelist in mesh.faces.surface_elements_to_nodes_maps[domain].values:
                        for node in nodelist:
                            if node < mesh.node_classes[1]:
                                domain_nodes.add(node)
                domain_nodes = list(domain_nodes)
                A.zero_rows(domain_nodes, 1.0)
                # Apply BC on RHS
                # FIXME: Need to generate the appropriate code for setting the BC depending
                # on the number of nodes and data dimension
                dim = len(bc.function_arg)
                value = op2.Global(dim, bc.function_arg)
                op2.par_loop(_apply_dirichlet[dim], iterset,
                             b(elem_map, op2.WRITE),
                             value(op2.READ))

        # FIXME: Parameters are not forced into solver a la Dolfin - only taken from FLML.
        _la_solve(A, self._problem.u, b)

class NonlinearVariationalSolver(object):
    """Solves a nonlinear variational problem."""
    pass

def _assemble_tensor(f):
    kernels = ffc_interface.compile_form(f, "form")

    fd = f.form_data()
    is_mat = fd.rank == 2

    if is_mat:
        test,trial = fd.arguments
        itspace_extents = [ arg.mesh.shape.loc for arg in (test, trial) ]
        
        # Construct OP2 Mat to assemble into
        trial_element = trial.element()
        if isinstance(trial_element, FiniteElement):
            sparsity_dim = 1
        elif isinstance(trial_element, VectorElement):
            sparsity_dim = trial_element.topological_dimension()
        else: # TensorElement
            sparsity_dim = pow(trial_element.topological_dimension(), 2)
        mesh_names = (test.mesh.name, trial.mesh.name)
        sparsity = op2.Sparsity((test.mesh.element_node_map, trial.mesh.element_node_map),
                                sparsity_dim, "%s_%s_sparsity" % mesh_names)
        tensor = op2.Mat(sparsity, numpy.float64, "%s_%s_matrix" % mesh_names)    
    else:
        test = fd.arguments[0]
        itspace_extents = [test.mesh.shape.loc]
        tensor = fd.coefficients[0].temporary_dat
        tensor.zero()

    # FIXME: put somewhere appropriate
    test.mesh.compute_boundaries()
    coords = test.mesh.coords

    for kernel, integral in zip(kernels, f.integrals()):
        domain_type = integral.measure().domain_type()
        if domain_type == 'cell':
            if is_mat:
                tensor_arg = tensor((test.mesh.element_node_map[op2.i[0]], 
                                     trial.mesh.element_node_map[op2.i[1]]), op2.INC)
            else:
                tensor_arg = tensor(test.mesh.element_node_map[op2.i[0]], op2.INC)
            itspace = test.mesh.element_set(*itspace_extents)
            args = [kernel, itspace, tensor_arg,
                    coords.dat(coords.element_node_map, op2.READ)]
            for c in fd.coefficients:
                args.append(c.dat(c.element_node_map, op2.READ))
            op2.par_loop(*args)
        if domain_type == 'exterior_facet':
            boundary = integral.measure().domain_id().subdomain_ids()[0]
            if boundary not in test.mesh.faces.boundary_list:
                iterset = op2.Set(0)
                elem_node = op2.Map(iterset, test.mesh.node_set,
                                    test.mesh.faces.surface_mesh.shape.loc,
                                    numpy.zeros(0))
                facet_dat = op2.Dat(iterset, 1, numpy.zeros(0),
                                    numpy.uint32,
                                    name='dummy_dat',
                                    uid = test.mesh.faces.surface_mesh.uid)
            else:
                iterset = test.mesh.faces.boundary_elem_sets[boundary]
                elem_node = test.mesh.faces.boundary_maps[boundary]
                facet_dat = test.mesh.faces.boundary_facets[boundary]
            if is_mat:
                tensor_arg = tensor((elem_node[op2.i[0]], 
                                     elem_node[op2.i[1]]), op2.INC)
            else:
                tensor_arg = tensor(elem_node[op2.i[0]], op2.INC)
            itspace = iterset(*itspace_extents)
            args = [kernel, itspace, tensor_arg,
                    coords.dat(elem_node, op2.READ)]
            for c in fd.coefficients:
                args.append(c.dat(elem_node, op2.READ))
            args.append(facet_dat(op2.IdentityMap, op2.READ))
            op2.par_loop(*args)
        if domain_type == 'interior_facet':
            raise NotImplementedError("Unsupported interior_facet integral.")
    
    return tensor

def _la_solve(A, x, b, linear_solver=None, preconditioner=None):
    """Solves a linear algebra problem. Usage:
    
    .. code-block:: python
    
        _la_solve(A, x, b, linear_solver=None, preconditioner=None)
        
    The linear solver and preconditioner are selected with the following
    precedence:
    
    1. Using linear_solver and preconditioner passed into the function. This is for
       source compatibility with Dolfin and would not normally be used in Fluidity.
    2. Using the solver options specified in the flml."""

    solver_path = "%s/prognostic/solver/iterative_method/name" % option_path
    preconditioner_path = "%s/prognostic/solver/preconditioner/name" % option_path
    rtol_path = "%s/prognostic/solver/relative_error" % option_path
    atol_path = "%s/prognostic/solver/absolute_error" % option_path
    maxit_path = "%s/prognostic/solver/max_iterations" % option_path
    ignore_failure_path = "%s/prognostic/solver/ignore_all_solver_failures" % option_path
    monitor_conv_path = "%s/prognostic/solver/diagnostics/monitors/preconditioned_residual" % option_path
    plot_conv_path = "%s/prognostic/solver/diagnostics/monitors/preconditioned_residual_graph" % option_path
   
    parameters = {
            'linear_solver': linear_solver  or get_option(solver_path),
            'preconditioner': preconditioner or get_option(preconditioner_path),
            'relative_tolerance': get_option(rtol_path),
            'maximum_iterations': get_option(maxit_path),
            'error_on_nonconvergence': not have_option(ignore_failure_path),
            'monitor_convergence': have_option(monitor_conv_path),
            'plot_convergence': have_option(plot_conv_path),
            'plot_prefix': get_option('/simulation_name')+'_'
            }
    if have_option(atol_path):
        parameters['absolute_tolerance'] = get_option(atol_path)
    solver = op2.Solver(parameters=parameters)
    solver.solve(A, x.dat, b)
    x.dat.halo_exchange_begin()
    x.dat.halo_exchange_end()

def solve(*args, **kwargs):
    """Solve linear system Ax = b or variational problem a == L or F == 0.

    The DOLFIN solve() function can be used to solve either linear
    systems or variational problems. The following list explains the
    various ways in which the solve() function can be used.

    *1. Solving linear systems*

    A linear system Ax = b may be solved by calling solve(A, x, b),
    where A is a matrix and x and b are vectors. Optional arguments
    may be passed to specify the solver method and preconditioner.
    Some examples are given below:

    .. code-block:: python

        solve(A, x, b)
        solve(A, x, b, "lu")
        solve(A, x, b, "gmres", "ilu")
        solve(A, x, b, "cg", "hypre_amg")

    Possible values for the solver method and preconditioner depend
    on which linear algebra backend is used and how that has been
    configured.

    *2. Solving linear variational problems*

    A linear variational problem a(u, v) = L(v) for all v may be
    solved by calling solve(a == L, u, ...), where a is a bilinear
    form, L is a linear form, u is a Function (the solution). Optional
    arguments may be supplied to specify boundary conditions or solver
    parameters. Some examples are given below:

    .. code-block:: python

        solve(a == L, u)
        solve(a == L, u, bcs=bc)
        solve(a == L, u, bcs=[bc1, bc2])

        solve(a == L, u, bcs=bcs,
              solver_parameters={"linear_solver": "lu"},
              form_compiler_parameters={"optimize": True})

    *3. Solving nonlinear variational problems*

    A nonlinear variational problem F(u; v) = 0 for all v may be
    solved by calling solve(F == 0, u, ...), where the residual F is a
    linear form (linear in the test function v but possibly nonlinear
    in the unknown u) and u is a Function (the solution). Optional
    arguments may be supplied to specify boundary conditions, the
    Jacobian form or solver parameters. If the Jacobian is not
    supplied, it will be computed by automatic differentiation of the
    residual form. Some examples are given below:

    .. code-block:: python

        solve(F == 0, u)
        solve(F == 0, u, bcs=bc)
        solve(F == 0, u, bcs=[bc1, bc2])

        solve(F == 0, u, bcs, J=J,
              solver_parameters={"linear_solver": "lu"},
              form_compiler_parameters={"optimize": True})

    """

    assert(len(args) > 0)

    # Call variational problem solver if we get an equation
    if isinstance(args[0], ufl.classes.Equation):
        _solve_varproblem(*args, **kwargs)

    # Default case, just call the wrapped C++ solve function
    else:
        if kwargs:
            log.error("In solving.py, "\
                      "solving linear algebra problem, "\
                      "Not expecting keyword arguments when solving "\
                      "linear algebra problem")
            
        return _la_solve(*args)

def _solve_varproblem(*args, **kwargs):
    "Solve variational problem a == L or F == 0"

    # Extract arguments
    eq, u, bcs, J, M, form_compiler_parameters, solver_parameters \
        = _extract_args(*args, **kwargs)

    # Solve linear variational problem
    if isinstance(eq.lhs, ufl.Form) and isinstance(eq.rhs, ufl.Form):

        # Create problem
        problem = LinearVariationalProblem(eq.lhs, eq.rhs, u, bcs,
                    form_compiler_parameters=form_compiler_parameters)

        # Create solver and call solve
        solver = LinearVariationalSolver(problem)
        solver.parameters.update(solver_parameters)
        solver.solve()

    # Solve nonlinear variational problem
    else:

        # Create Jacobian if missing
        if J is None:
            log.info("No Jacobian form specified for nonlinear variational problem.")
            log.info("Differentiating residual form F to obtain Jacobian J = F'.")
            F = eq.lhs
            J = ufl.derivative(F, u)

        # Create problem
        problem = NonlinearVariationalProblem(eq.lhs, u, bcs, J,
                    form_compiler_parameters=form_compiler_parameters)

        # Create solver and call solve
        solver = NonlinearVariationalSolver(problem)
        solver.parameters.update(solver_parameters)
        solver.solve()

def _extract_args(*args, **kwargs):
    "Extraction of arguments for _solve_varproblem"

    # Check for use of valid kwargs
    valid_kwargs = ["bcs", "J", "M",
                    "form_compiler_parameters", "solver_parameters"]
    for kwarg in kwargs.iterkeys():
        if not kwarg in valid_kwargs:
            log.error("solving.py, "\
                      "solving variational problem, "\
                      "Illegal keyword argument \"%s\"; valid keywords are %s" % \
                           (kwarg,
                            ", ".join("\"%s\"" % kwarg for kwarg in valid_kwargs)))

    # Extract equation
    if not len(args) >= 2:
        log.error("solving.py, "\
                  "solving variational problem, "\
                  "Missing arguments, expecting solve(lhs == rhs, "\
                  "u, bcs=bcs), where bcs is optional")
    if len(args) > 3:
        log.error("solving.py, "\
                  "solving variational problem, "\
                  "Too many arguments, expecting solve(lhs == rhs, "\
                  "u, bcs=bcs), where bcs is optional")

    # Extract equation
    eq = _extract_eq(args[0])

    # Extract solution function
    u = _extract_u(args[1])

    # Extract boundary conditions
    if len(args) > 2:
        bcs = _extract_bcs(args[2])
    elif "bcs" in kwargs:
        bcs = _extract_bcs(kwargs["bcs"])
    else:
        bcs = []

    # Extract Jacobian
    J = kwargs.get("J", None)
    if J is not None and not isinstance(J, ufl.Form):
        log.error("solving.py, "\
                  "solving variational problem, "\
                  "Expecting Jacobian J to be a UFL Form")

    # Extract functional
    M = kwargs.get("M", None)
    if M is not None and not isinstance(M, ufl.Form):
        log.error("solving.py, "\
                  "solving variational problem, "\
                  "Expecting goal functional M to be a UFL Form")

    # Extract parameters
    form_compiler_parameters = kwargs.get("form_compiler_parameters", {})
    solver_parameters = kwargs.get("solver_parameters", {})

    return eq, u, bcs, J, M, form_compiler_parameters, solver_parameters

def _extract_eq(eq):
    "Extract and check argument eq"
    if not isinstance(eq, ufl.classes.Equation):
        log.error("solving.py, "\
                  "solving variational problem, "\
                  "Expecting first argument to be an Equation")
    return eq

def _extract_u(u):
    "Extract and check argument u"
    if not isinstance(u, ufl.Coefficient):
        log.error("solving.py, "\
                  "solving variational problem, "\
                  "Expecting second argument to be a Coefficient")
    return u

def _extract_bcs(bcs):
    "Extract and check argument bcs"
    if bcs is None:
        bcs = []
    elif not isinstance(bcs, (list, tuple)):
        bcs = [bcs]
    for bc in bcs:
        if not isinstance(bc, BoundaryCondition):
            log.error("solving.py, "\
                      "solving variational problem, "\
                      "Unable to extract boundary condition arguments")
    return bcs