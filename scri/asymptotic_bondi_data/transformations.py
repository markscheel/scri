import math
import numpy as np
import quaternion
import spinsfast
import spherical_functions as sf


def _process_transformation_kwargs(input_ell_max, **kwargs):
    original_kwargs = kwargs.copy()

    # Build the supertranslation and spacetime_translation arrays
    supertranslation = np.zeros((4,), dtype=complex)  # For now; may be resized below
    ell_max_supertranslation = 1  # For now; may be increased below
    if 'supertranslation' in kwargs:
        supertranslation = np.array(kwargs.pop('supertranslation'), dtype=complex)
        if supertranslation.dtype != 'complex' and supertranslation.size > 0:
            # I don't actually think this can ever happen...
            raise TypeError("Input argument `supertranslation` should be a complex array with size>0.  "
                            f"Got a {supertranslation.dtype} array of shape {supertranslation.shape}")
        # Make sure the array has size at least 4, by padding with zeros
        if supertranslation.size <= 4:
            supertranslation = np.lib.pad(supertranslation, (0, 4-supertranslation.size),
                                          'constant', constant_values=(0.0,))
        # Check that the shape is a possible array of scalar modes with complete (ell,m) data
        ell_max_supertranslation = int(np.sqrt(len(supertranslation))) - 1
        if (ell_max_supertranslation + 1)**2 != len(supertranslation):
            raise ValueError("Input supertranslation parameter must contain modes from ell=0 up to some ell_max, "
                             "including\n           all relevant m modes in standard order (see `spherical_functions` "
                             "documentation for details).\n           Thus, it must be an array with length given by a "
                             "perfect square; its length is {len(supertranslation)}")
        # Check that the resulting supertranslation will be real
        for ell in range(ell_max_supertranslation+1):
            for m in range(ell+1):
                i_pos = sf.LM_index(ell, m, 0)
                i_neg = sf.LM_index(ell, -m, 0)
                a = supertranslation[i_pos]
                b = supertranslation[i_neg]
                if abs(a - (-1.)**m * b.conjugate()) > 3e-16 + 1e-15 * abs(b):
                    raise ValueError("\nsupertranslation[{0}]={1}  # (ell,m)=({2},{3})\n".format(i_pos, a, ell, m)
                                     + "supertranslation[{0}]={1}  # (ell,m)=({2},{3})\n".format(i_neg, b, ell, -m)
                                     + "Will result in an imaginary supertranslation.")
    spacetime_translation = np.zeros((4,), dtype=float)
    spacetime_translation[0] = sf.constant_from_ell_0_mode(supertranslation[0]).real
    spacetime_translation[1:4] = -sf.vector_from_ell_1_modes(supertranslation[1:4]).real
    if 'spacetime_translation' in kwargs:
        st_trans = np.array(kwargs.pop('spacetime_translation'), dtype=float)
        if st_trans.shape != (4,) or st_trans.dtype != 'float':
            raise TypeError("\nInput argument `spacetime_translation` should be a float array of shape (4,).\n"
                            "Got a {0} array of shape {1}.".format(st_trans.dtype, st_trans.shape))
        spacetime_translation = st_trans[:]
        supertranslation[0] = sf.constant_as_ell_0_mode(spacetime_translation[0])
        supertranslation[1:4] = sf.vector_as_ell_1_modes(-spacetime_translation[1:4])
    if 'space_translation' in kwargs:
        s_trans = np.array(kwargs.pop('space_translation'), dtype=float)
        if s_trans.shape != (3,) or s_trans.dtype != 'float':
            raise TypeError("\nInput argument `space_translation` should be an array of floats of shape (3,).\n"
                            "Got a {0} array of shape {1}.".format(s_trans.dtype, s_trans.shape))
        spacetime_translation[1:4] = s_trans[:]
        supertranslation[1:4] = sf.vector_as_ell_1_modes(-spacetime_translation[1:4])
    if 'time_translation' in kwargs:
        t_trans = kwargs.pop('time_translation')
        if not isinstance(t_trans, float):
            raise TypeError("Input argument `time_translation` should be a single float.  "
                            f"Got {t_trans}")
        spacetime_translation[0] = t_trans
        supertranslation[0] = sf.constant_as_ell_0_mode(spacetime_translation[0])

    # Decide on the number of points to use in each direction.  A nontrivial supertranslation will introduce
    # power in higher modes, so for best accuracy, we need to account for that.  But we'll make it a firm
    # requirement to have enough points to capture the original waveform, at least
    output_ell_max = kwargs.pop('output_ell_max', input_ell_max)
    working_ell_max = kwargs.pop('working_ell_max', 2*input_ell_max + ell_max_supertranslation)
    if working_ell_max < input_ell_max:
        raise ValueError(f"working_ell_max={working_ell_max} is too small; it must be at least ell_max={input_ell_max}")

    # Get the rotor for the frame rotation
    frame_rotation = np.quaternion(*np.array(kwargs.pop('frame_rotation', [1, 0, 0, 0]), dtype=float))
    if frame_rotation.abs() < 3e-16:
        raise ValueError(f"frame_rotation={frame_rotation} should be a single unit quaternion")
    frame_rotation = frame_rotation.normalized()

    # Get the boost velocity vector
    boost_velocity = np.array(kwargs.pop('boost_velocity', [0.0]*3), dtype=float)
    beta = np.linalg.norm(boost_velocity)
    if boost_velocity.dtype != float or boost_velocity.shape != (3,) or beta >= 1.0:
        raise ValueError(f"Input boost_velocity=`{boost_velocity}` should be a 3-vector with "
                         "magnitude strictly less than 1.0")

    return frame_rotation, boost_velocity, supertranslation, working_ell_max, output_ell_max


def boosted_grid(frame_rotation, boost_velocity, n_theta, n_phi):
    beta = np.linalg.norm(boost_velocity)
    gamma = 1 / math.sqrt(1 - beta**2)
    rapidity = math.atanh(beta)

    # Construct the function that modifies our rotor grid to account for the boost
    if beta > 3e-14:  # Tolerance for beta; any smaller and numerical errors will have greater effect
        vhat = boost_velocity / beta

        def Bprm_j_k(thetaprm, phiprm):
            """Construct rotor taking r' to r

            I derived this result in a different way, but I've also found it described in Penrose-Rindler Vol. 1,
            around Eq. (1.3.5).  Note, however, that their discussion is for the past celestial sphere,
            so there's a sign difference.

            """
            # Note: It doesn't matter which we use -- r' or r; all we need is the direction of the bivector
            # spanned by v and r', which is the same as the direction of the bivector spanned by v and r,
            # since either will be normalized, and one cross product is zero iff the other is zero.
            rprm = np.array([math.cos(phiprm)*math.sin(thetaprm),
                             math.sin(phiprm)*math.sin(thetaprm),
                             math.cos(thetaprm)])
            Thetaprm = math.acos(np.dot(vhat, rprm))
            Theta = 2 * math.atan(math.exp(-rapidity) * math.tan(Thetaprm/2.0))
            rprm_cross_vhat = np.quaternion(0.0, *np.cross(rprm, vhat))
            if rprm_cross_vhat.abs() > 1e-200:
                return (rprm_cross_vhat.normalized() * (Thetaprm - Theta) / 2).exp()
            else:
                return quaternion.one
    else:
        def Bprm_j_k(thetaprm, phiprm):
            return quaternion.one

    # These are the angles in the transformed system at which we need to know the function values
    thetaprm_phiprm = sf.theta_phi(n_theta, n_phi)

    # Set up rotors that we can use to evaluate the SWSHs in the original frame
    R_j_k = np.empty((n_theta, n_phi), dtype=np.quaternion)
    for j in range(n_theta):
        for k in range(n_phi):
            thetaprm_j, phiprm_k = thetaprm_phiprm[j, k]
            R_j_k[j, k] = (Bprm_j_k(thetaprm_j, phiprm_k)
                           * frame_rotation
                           * quaternion.from_spherical_coords(thetaprm_j, phiprm_k))

    return R_j_k


def transform(self, **kwargs):
    """Apply BMS transformation to AsymptoticBondiData object

    It is important to note that the input transformation parameters are applied in this order:

      1. (Super)Translations
      2. Rotation (about the origin)
      3. Boost (about the origin)

    All input parameters refer to the transformation required to take the input data's inertial
    frame onto the inertial frame of the output data's inertial observers.  In what follows, the
    coordinates of and functions in the input inertial frame will be unprimed, while corresponding
    values of the output inertial frame will be primed.

    The translations (space, time, spacetime, or super) can be given in various ways, which may
    override each other.  Ultimately, however, they are essentially combined into a single function
    `α`, representing the supertranslation, which transforms the asymptotic time variable `u` as

        u'(u, θ, ϕ) = u(u, θ, ϕ) - α(θ, ϕ)

    A simple time translation by δt would correspond to

        α(θ, ϕ) = δt  # Independent of (θ, ϕ)

    A pure spatial translation δx ·would correspond to

        α(θ, ϕ) = -δx · n̂(θ, ϕ)

    where `·` is the usual dot product, and `n̂` is the unit vector in the given direction.


    Parameters
    ----------
    abd: AsymptoticBondiData
        The object storing the modes of the original data, which will be transformed in this
        function.  This is the only required argument to this function.
    time_translation: float, optional
        Defaults to zero.  Nonzero overrides corresponding components of spacetime_translation and
        supertranslation.
    space_translation : float array of length 3, optional
        Defaults to empty (no translation).  Non-empty overrides corresponding components of
        spacetime_translation and supertranslation.
    spacetime_translation : float array of length 4, optional
        Defaults to empty (no translation).  Non-empty overrides corresponding components of
        supertranslation.
    supertranslation : complex array, optional
        This gives the complex components of the spherical-harmonic expansion of the
        supertranslation in standard form, starting from ell=0 up to some ell_max, which may be
        different from the ell_max of the input `abd` object.  Supertranslations must be real, so
        these values should obey the condition
            α^{ℓ,m} = (-1)^m ᾱ^{ℓ,-m}
        This condition is actually imposed on the input data, so imaginary parts of α(θ, ϕ) will
        essentially be discarded.  Defaults to empty, which causes no supertranslation.
    frame_rotation : quaternion, optional
        Transformation applied to (x,y,z) basis of the input mode's inertial frame.  For example,
        the basis z vector of the new frame may be written as
           z' = frame_rotation * z * frame_rotation.inverse()
        Defaults to 1, corresponding to the identity transformation (no rotation).
    boost_velocity : float array of length 3, optional
        This is the three-velocity vector of the new frame relative to the input frame.  The norm of
        this vector is checked to ensure that it is smaller than 1.  Defaults to [], corresponding
        to no boost.

    Returns
    -------
    AsymptoticBondiData

    """
    from scipy.interpolate import CubicSpline

    # Parse the input arguments, and define the basic parameters for this function
    frame_rotation, boost_velocity, supertranslation, working_ell_max, output_ell_max,  = \
        _process_transformation_kwargs(self.ell_max, **kwargs)
    n_theta = 2 * working_ell_max + 1
    n_phi = n_theta
    β = np.linalg.norm(boost_velocity)
    γ = 1 / math.sqrt(1 - β**2)

    # Make this into a Modes object, so it can keep track of its spin weight, etc., through the
    # various operations needed below.
    supertranslation = sf.Modes(supertranslation, spin_weight=0).real

    # This is a 2-d array of unit quaternions, which are what the spin-weighted functions should be
    # evaluated on (even for spin 0 functions, for simplicity).  That will be equivalent to
    # evaluating the spin-weighted functions with respect to the transformed grid -- although on the
    # original time slices.
    distorted_grid_rotors = boosted_grid(frame_rotation, boost_velocity, n_theta, n_phi)

    # Compute u, α, ðα, ððα, v·r, 1/k, 1/k**3, k, and ðk/k on the distorted grid, including new axes
    # to enable broadcasting with time-dependent functions.  Note that the first axis should
    # represent variation in u, the second axis variation along θ', and the third axis variation
    # along ϕ'.
    u = self.u[:, np.newaxis, np.newaxis]
    α = supertranslation.evaluate(distorted_grid_rotors).real[np.newaxis, :, :]
    ðα = supertranslation.eth.evaluate(distorted_grid_rotors)[np.newaxis, :, :]
    ððα = supertranslation.eth.eth.evaluate(distorted_grid_rotors)[np.newaxis, :, :]
    v_dot_r = np.array([
        [np.dot(boost_velocity, (R*quaternion.z*R.conjugate()).vec) for R in dgr]
        for dgr in distorted_grid_rotors
    ])[np.newaxis, :, :]
    one_over_k = γ * (1 - v_dot_r)
    one_over_k_cubed = one_over_k**3
    k = 1.0 / one_over_k
    ðk_over_k = math.sqrt(2) * γ * v_dot_r * k

    # ðu'(u, θ', ϕ') exp(iλ) / k(θ', ϕ')
    ðuprime_over_k = ðk_over_k * (u - α) - ðα

    # ψ0(u, θ', ϕ') exp(2iλ)
    ψ0 = self.psi0.evaluate(distorted_grid_rotors)
    # ψ1(u, θ', ϕ') exp(iλ)
    ψ1 = self.psi1.evaluate(distorted_grid_rotors)
    # ψ2(u, θ', ϕ')
    ψ2 = self.psi2.evaluate(distorted_grid_rotors)
    # ψ3(u, θ', ϕ') exp(-1iλ)
    ψ3 = self.psi3.evaluate(distorted_grid_rotors)
    # ψ4(u, θ', ϕ') exp(-2iλ)
    ψ4 = self.psi4.evaluate(distorted_grid_rotors)
    # σ(u, θ', ϕ') exp(2iλ)
    σ = self.sigma.evaluate(distorted_grid_rotors)

    ### The following calculations are done using in-place Horner form.  I suspect this will be the
    ### most efficient form of this calculation, within reason.  Note that the factors of exp(isλ)
    ### were computed automatically by evaluating in terms of quaternions.
    #
    fprime_of_timenaught_directionprime = np.empty((6, self.n_times, n_theta, n_phi), dtype=complex)
    # ψ0'(u, θ', ϕ')
    fprime_of_timenaught_directionprime[0] = ψ4
    fprime_of_timenaught_directionprime[0] *= ðuprime_over_k
    fprime_of_timenaught_directionprime[0] += -4 * ψ3
    fprime_of_timenaught_directionprime[0] *= ðuprime_over_k
    fprime_of_timenaught_directionprime[0] += 6 * ψ2
    fprime_of_timenaught_directionprime[0] *= ðuprime_over_k
    fprime_of_timenaught_directionprime[0] += -4 * ψ1
    fprime_of_timenaught_directionprime[0] *= ðuprime_over_k
    fprime_of_timenaught_directionprime[0] += ψ0
    fprime_of_timenaught_directionprime[0] *= one_over_k_cubed
    # ψ1'(u, θ', ϕ')
    fprime_of_timenaught_directionprime[1] = -ψ4
    fprime_of_timenaught_directionprime[1] *= ðuprime_over_k
    fprime_of_timenaught_directionprime[1] += 3 * ψ3
    fprime_of_timenaught_directionprime[1] *= ðuprime_over_k
    fprime_of_timenaught_directionprime[1] += -3 * ψ2
    fprime_of_timenaught_directionprime[1] *= ðuprime_over_k
    fprime_of_timenaught_directionprime[1] += ψ1
    fprime_of_timenaught_directionprime[1] *= one_over_k_cubed
    # ψ2'(u, θ', ϕ')
    fprime_of_timenaught_directionprime[2] = ψ4
    fprime_of_timenaught_directionprime[2] *= ðuprime_over_k
    fprime_of_timenaught_directionprime[2] += -2 * ψ3
    fprime_of_timenaught_directionprime[2] *= ðuprime_over_k
    fprime_of_timenaught_directionprime[2] += ψ2
    fprime_of_timenaught_directionprime[2] *= one_over_k_cubed
    # ψ3'(u, θ', ϕ')
    fprime_of_timenaught_directionprime[3] = -ψ4
    fprime_of_timenaught_directionprime[3] *= ðuprime_over_k
    fprime_of_timenaught_directionprime[3] += ψ3
    fprime_of_timenaught_directionprime[3] *= one_over_k_cubed
    # ψ4'(u, θ', ϕ')
    fprime_of_timenaught_directionprime[4] = ψ4
    fprime_of_timenaught_directionprime[4] *= one_over_k_cubed
    # σ'(u, θ', ϕ')
    fprime_of_timenaught_directionprime[5] = one_over_k * (σ - ððα)

    # Determine the new time slices.  The set timeprime is chosen so that on each slice of constant
    # u'_i, the average value of u=u'/k+α is precisely <u>=u'γ+<α>=u_i.  But then, we have to narrow
    # that set down, so that every physical point on all the u'_i' slices correspond to data in the
    # range of input data.
    timeprime = (u[:, 0, 0] - sf.constant_from_ell_0_mode(supertranslation[0]).real) / γ
    timeprime_of_initialtime_directionprime = (k * (u[0] - α))
    timeprime_of_finaltime_directionprime = (k * (u[-1] - α))
    earliest_complete_timeprime = np.max(timeprime_of_initialtime_directionprime)
    latest_complete_timeprime = np.min(timeprime_of_finaltime_directionprime)
    timeprime = timeprime[(timeprime >= earliest_complete_timeprime)
                          & (timeprime <= latest_complete_timeprime)]

    # This will store the values of f'(u', θ', ϕ') for the various functions `f`
    fprime_of_timeprime_directionprime = np.zeros((6, timeprime.size, n_theta, n_phi), dtype=complex)

    # Interpolate the various transformed function values on the transformed grid from the original
    # time coordinate to the new set of time coordinates, independently for each direction.
    for i in range(n_theta):
        for j in range(n_phi):
            k_i_j = k[0, i, j]
            α_i_j = α[0, i, j]
            # u'(u, θ', ϕ')
            timeprime_of_timenaught_directionprime_i_j = k_i_j * (self.u - α_i_j)
            # f'(u', θ', ϕ')
            fprime_of_timeprime_directionprime[:, :, i, j] = CubicSpline(
                timeprime_of_timenaught_directionprime_i_j,
                fprime_of_timenaught_directionprime[:, :, i, j],
                axis=1
            )(timeprime)

    abdprime = type(self)(timeprime, output_ell_max)
    # ψ0'(u')_{ell', m'}
    abdprime.psi0 = spinsfast.map2salm(fprime_of_timeprime_directionprime[0], 2, output_ell_max)
    # ψ1'(u')_{ell', m'}
    abdprime.psi1 = spinsfast.map2salm(fprime_of_timeprime_directionprime[1], 1, output_ell_max)
    # ψ2'(u')_{ell', m'}
    abdprime.psi2 = spinsfast.map2salm(fprime_of_timeprime_directionprime[2], 0, output_ell_max)
    # ψ3'(u')_{ell', m'}
    abdprime.psi3 = spinsfast.map2salm(fprime_of_timeprime_directionprime[3], -1, output_ell_max)
    # ψ4'(u')_{ell', m'}
    abdprime.psi4 = spinsfast.map2salm(fprime_of_timeprime_directionprime[4], -2, output_ell_max)
    # σ'(u')_{ell', m'}
    abdprime.sigma = spinsfast.map2salm(fprime_of_timeprime_directionprime[5], 2, output_ell_max)

    return abdprime
