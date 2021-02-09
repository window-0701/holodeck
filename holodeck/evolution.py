"""
"""

import enum
import abc

import numpy as np

from holodeck import utils, cosmo
from holodeck.constants import GYR

_DEF_TIME_DELAY = (5.0*GYR, 0.2)


@enum.unique
class EVO(enum.Enum):
    CONT = 1
    END = -1


class _Binary_Evolution(abc.ABC):

    _EVO_PARS = ['mass', 'sepa', 'eccs', 'redz', 'dadt', 'tlbk']
    _LIN_INTERP_PARS = ['eccs', 'redz', 'tlbk']
    _SELF_CONSISTENT = None

    def __init__(self, bin_pop, nsteps=100, mods=None, check=True):
        self._bin_pop = bin_pop
        self._nsteps = nsteps
        self._mods = mods

        size = bin_pop.size
        shape = (size, nsteps)

        self._shape = shape
        self.time = np.zeros(shape)
        self.tlbk = np.zeros(shape)
        self.sepa = np.zeros(shape)
        self.mass = np.zeros(shape + (2,))

        if bin_pop.eccs is not None:
            self.eccs = np.zeros(shape)
            self.dedt = np.zeros(shape)
        else:
            self.eccs = None
            self.dedt = None

        # NOTE: these values should be stored as positive values
        self.dadt = np.zeros(shape)

        # Derived parameters
        self._freq_orb_rest = None
        self._evolved = False
        self._coal = None

        return

    @property
    def shape(self):
        return self._shape

    def evolve(self):
        # ---- Initialize Integration Step Zero
        self._init_step_zero()

        # ---- Iterate through all integration steps
        size, nsteps = self.shape
        steps_list = range(1, nsteps)
        for step in steps_list:
            rv = self._take_next_step(step)
            if rv is EVO.END:
                self._evolved = True
                break
            elif rv not in EVO:
                raise ValueError("Recieved bad `rv` ({}) after step {}!".format(rv, step))

        # ---- Finalize
        self._finalize()
        return

    @property
    def coal(self):
        if self._coal is None:
            self._coal = (self.redz[:, -1] > 0.0)
        return self._coal

    def _init_step_zero(self):
        bin_pop = self._bin_pop
        _, nsteps = self.shape

        # Initialize ALL separations ranging from initial to mutual-ISCO, for each binary
        rad_isco = utils.rad_isco(*bin_pop.mass.T)
        # (2, N)
        sepa = np.log10([bin_pop.sepa, rad_isco])
        # Get log-space range of separations for each of N ==> (N, nsteps)
        sepa = np.apply_along_axis(lambda xx: np.logspace(*xx, nsteps), 0, sepa).T
        self.sepa[:, :] = sepa

        self.time[:, 0] = bin_pop.time
        redz = utils.a_to_z(bin_pop.time)
        tlbk = cosmo.z_to_tlbk(redz)
        self.tlbk[:, 0] = tlbk
        self.mass[:, 0, :] = bin_pop.mass

        if (bin_pop.eccs is not None):
            self.eccs[:, 0] = bin_pop.eccs

        return

    @abc.abstractmethod
    def _take_next_step(self, ii):
        pass

    def _check(self):
        _check_var_names = ['sepa', 'time', 'mass', 'tlbk', 'dadt']
        _check_var_names_eccs = ['eccs', 'dedt']

        def check_vars(names):
            for cv in names:
                vals = getattr(self, cv)
                if np.any(~np.isfinite(vals)):
                    err = "Found non-finite '{}' !".format(cv)
                    raise ValueError(err)

        check_vars(_check_var_names)

        if self.eccs is None:
            return

        check_vars(_check_var_names_eccs)

        return

    def _finalize(self):
        self._evolved = True

        self.modify()

        # Run diagnostics
        self._check()

        return

    def modify(self, mods=None):
        self._check_evolved()

        if mods is None:
            mods = self._mods

        # Sanitize
        if mods is None:
            mods = []
        elif not isinstance(mods, list):
            mods = [mods]

        # Run Modifiers
        for mod in mods:
            mod(self)

        # Update derived quantites (following modifications)
        self._update_derived()
        return

    def _update_derived(self):
        pass

    @property
    def freq_orb_rest(self):
        if self._freq_orb_rest is None:
            self._check_evolved()
            mtot = self.mass.sum(axis=-1)
            self._freq_orb_rest = utils.kepler_freq_from_sep(mtot, self.sepa)
        return self._freq_orb_rest

    @property
    def freq_orb_obs(self):
        redz = utils.a_to_z(self.time)
        fobs = self.freq_orb_rest / (1.0 + redz)
        return fobs

    def _check_evolved(self):
        if self._evolved is not True:
            raise RuntimeError("This instance has not been evolved yet!")

        return

    def at(self, xpar, targets, pars=None, coal=False):
        """Interpolate evolution to the given observed, orbital frequency.

        Arguments
        ---------
        xpar : str, one of ['fobs', 'sepa']
            String specifying the variable to interpolate to.
        targets : array of scalar
            Locations to interpolate to.
            `sepa` : units of cm
            `fobs` : units of 1/s [Hz]

        Out of bounds values are set to `np.nan`.
        Interpolation is 1st-order in log-log space.

        """
        self._check_evolved()
        _allowed = ['sepa', 'fobs']
        if xpar not in _allowed:
            raise ValueError("`xpar` must be one of '{}'!".format(_allowed))

        if pars is None:
            pars = self._EVO_PARS
        if np.isscalar(pars):
            pars = [pars]
        squeeze = False
        if np.isscalar(targets):
            targets = np.atleast_1d(targets)
            squeeze = True

        size, nsteps = self.shape
        # Observed-Frequency, units of 1/yr
        if xpar == 'fobs':
            # frequency is already increasing
            _xvals = np.log10(self.freq_orb_obs)
            time = self.time[:, :]
            tt = np.log10(targets)
            rev = False
        # Binary-Separation, units of pc
        elif xpar == 'sepa':
            # separation is decreasing, reverse to increasing
            _xvals = np.log10(self.sepa)[:, ::-1]
            time = self.time[:, ::-1]
            tt = np.log10(targets)
            rev = True
        else:
            raise ValueError("Bad `xpar` {}!".format(xpar))

        # Find the evolution-steps immediately before and after the target frequency
        textr = utils.minmax(tt)
        xextr = utils.minmax(_xvals)
        if (textr[1] < xextr[0]) | (textr[0] > xextr[1]):
            err = "`targets` extrema ({}) ourside `xvals` extema ({})!  Bad units?".format(
                (10.0**textr), (10.0**xextr))
            raise ValueError(err)

        # Convert to (N, T, M)
        #     `tt` is (T,)  `xvals` is (N, M) for N-binaries and M-steps
        select = (tt[np.newaxis, :, np.newaxis] <= _xvals[:, np.newaxis, :])
        # Select only binaries that coalesce before redshift zero (a=1.0)
        if coal:
            select = select & (time[:, np.newaxis, :] > 0.0) & (time[:, np.newaxis, :] < 1.0)

        # (N, T), find the index of the xvalue following each target point (T,), for each binary (N,)
        aft = np.argmax(select, axis=-1)
        # zero values in `aft` mean no xvals after the targets were found
        valid = (aft > 0)
        bef = np.copy(aft)
        bef[valid] -= 1

        # (2, N, T)
        cut = np.array([aft, bef])
        # Valid binaries must be valid at both `bef` and `aft` indices
        for cc in cut:
            valid = valid & np.isfinite(np.take_along_axis(time, cc, axis=-1))

        inval = ~valid
        # Get the x-values before and after the target locations  (2, N, T)
        xvals = [np.take_along_axis(_xvals, cc, axis=-1) for cc in cut]
        # Find how far to interpolate between values (in log-space)
        #     (N, T)
        frac = (tt[np.newaxis, :] - xvals[1]) / np.subtract(*xvals)

        vals = dict()
        # Interpolate each target parameter
        for pp in pars:
            lin_interp = (pp in self._LIN_INTERP_PARS)
            # Either (N, M) or (N, M, 2)
            _data = getattr(self, pp)
            if _data is None:
                vals[pp] = None
                continue

            reshape = False
            use_cut = cut
            use_frac = frac
            # Sometimes there is a third dimension for the 2 binaries (e.g. `mass`)
            #    which will have shape, (N, T, 2) --- calling this "double-data"
            if np.ndim(_data) != 2:
                if (np.ndim(_data) == 3) and (np.shape(_data)[-1] == 2):
                    # Keep the interpolation axis last (N, T, 2) ==> (N, 2, T)
                    _data = np.moveaxis(_data, -1, -2)
                    # Expand other arrays appropriately
                    use_cut = cut[:, :, np.newaxis]
                    use_frac = frac[:, np.newaxis, :]
                    reshape = True
                else:
                    raise ValueError("Unexpected shape of data: {}!".format(np.shape(_data)))

            if not lin_interp:
                _data = np.log10(_data)

            if rev:
                _data = _data[..., ::-1]
            # (2, N, T) for scalar data or (2, N, 2, T) for "double-data"
            data = [np.take_along_axis(_data, cc, axis=-1) for cc in use_cut]
            # Interpolate by `frac` for each binary   (N, T) or (N, 2, T) for "double-data"
            new = data[1] + (np.subtract(*data) * use_frac)
            # In the "double-data" case, move the doublet back to the last dimension
            #    (N, T) or (N, T, 2)
            if reshape:
                new = np.moveaxis(new, 1, -1)
            # Set invalid binaries to nan
            new[inval, ...] = np.nan

            if np.any(~np.isfinite(new[valid, ...])):
                raise ValueError("Non-finite values after interpolation of '{}'".format(pp))

            # fill return dictionary
            if not lin_interp:
                new = 10.0 ** new
            if squeeze:
                new = new.squeeze()
            vals[pp] = new

            if np.any(~np.isfinite(new[valid, ...])):
                raise ValueError("Non-finite values after exponentiation of '{}'".format(pp))

        return vals


class BE_Magic_Delay(_Binary_Evolution):

    _SELF_CONSISTENT = False

    def __init__(self, *args, time_delay=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._time_delay = utils._parse_log_norm_pars(time_delay, self.shape[0], default=_DEF_TIME_DELAY)
        return

    def _init_step_zero(self):
        super()._init_step_zero()
        nbins, nsteps = self.shape
        mass = self.mass[:, 0, :]
        tlbk = self.tlbk[:, 0]
        sepa = self.sepa
        m1, m2 = mass.T

        # Time delay (N,)
        dtime = self._time_delay
        # Instantly step-forward this amount, discontinuously
        tlbk = np.clip(tlbk - dtime, 0.0, None)
        self.tlbk[:, 1:] = tlbk[:, np.newaxis]
        # calculate new scalefactors
        redz = cosmo.tlbk_to_z(tlbk)
        self.time[:, 1:] = utils.z_to_a(redz)[:, np.newaxis]

        # Masses don't evolve
        self.mass[:, :, :] = mass[:, np.newaxis, :]
        self.dadt[...] = - utils.gw_hardening_rate_dadt(m1[:, np.newaxis], m2[:, np.newaxis], sepa)
        return

    def _take_next_step(self, step):
        return EVO.END
