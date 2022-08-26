"""Gravitational Wave (GW) calculations module.

This module provides tools for calculating GW signals from MBH binaries.
Currently the components here are used with the 'discrete' / 'illustris' population of binaries,
and not the semi-analytic or observational population models.

"""

import numba
import numpy as np

import kalepy as kale

import holodeck as holo
from holodeck import utils, cosmo, log
# from holodeck.constants import MPC, MSOL


_CALC_MC_PARS = ['mass', 'sepa', 'dadt', 'scafa', 'eccen']


class Grav_Waves:

    def __init__(self, bin_evo, fobs_gw, nharms=30, nreals=100):
        self.fobs_gw = fobs_gw
        self.nharms = nharms
        self.nreals = nreals
        self._bin_evo = bin_evo
        return


class GW_Discrete(Grav_Waves):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._box_vol_cgs = self._bin_evo._sample_volume

        dlnf = np.diff(np.log(self.fobs_gw))
        if not np.allclose(dlnf[0], dlnf):
            log.exception("`GW_Discrete` will not work properly with unevenly sampled frequency (log-space)!")

        return

    def emit(self, eccen=None, stats=False, progress=True, nloudest=5):
        fobs_gw = self.fobs_gw
        nharms = self.nharms
        nreals = self.nreals
        bin_evo = self._bin_evo
        box_vol = self._box_vol_cgs

        if eccen is None:
            eccen = (bin_evo.eccen is not None)

        if eccen not in [True, False]:
            raise ValueError("`eccen` '{}' is invalid!".format(eccen))

        loudest = np.zeros((fobs_gw.size, nloudest, nreals))
        fore = np.zeros((fobs_gw.size, nreals))
        back = np.zeros((fobs_gw.size, nreals))
        both = np.zeros((fobs_gw.size, nreals))

        if eccen:
            harm_range = range(1, nharms+1)
        else:
            harm_range = [2]

        freq_iter = enumerate(fobs_gw)
        freq_iter = utils.tqdm(freq_iter, total=len(fobs_gw), desc='GW frequencies') if progress else freq_iter
        for ii, fogw in freq_iter:
            lo = fobs_gw[0] if (ii == 0) else fobs_gw[ii-1]
            hi = fobs_gw[1] if (ii == 0) else fobs_gw[ii]
            dlnf = np.log(hi) - np.log(lo)
            _both, _fore, _back, _loud = _gws_harmonics_at_evo_fobs(
                fogw, dlnf, bin_evo, harm_range, nreals, box_vol, loudest=nloudest
            )
            loudest[ii, :] = _loud
            both[ii, :] = _both
            fore[ii, :] = _fore
            back[ii, :] = _back

        self.both = np.sqrt(both)
        self.fore = np.sqrt(fore)
        self.back = np.sqrt(back)
        self.strain = np.sqrt(back + fore)
        self.loudest = loudest
        return


'''
def _calc_mc_at_fobs(fogw, _harms, nreals, bin_evo, box_vol, loudest=5):
    """
    """
    fo_orb = fo_gw / 2.0
    data_harms = bin_evo.at('fobs', fo_orb, params=_CALC_MC_PARS)

    redz = cosmo.a_to_z(data_harms['scafa'])
    valid = (redz > 0.0)
    redz = redz[valid]
    dcom = cosmo.z_to_dcom(redz)
    zp1 = redz + 1
    fr_orb = utils.frst_from_fobs(fo_orb, redz)
    mchirp = data_harms['mass'][valid]
    mchirp = utils.chirp_mass(*mchirp.T)
    hs2 = utils.gw_strain_source(mchirp, dcom, fr_orb)**2

    dfdt, _ = utils.dfdt_from_dadt(data_harms['dadt'][valid], data_harms['sepa'][valid], frst_orb=fr_orb)
    tfac = fr_orb / dfdt
    vfac = 4.0*np.pi*SPLC * zp1 * dcom**2 / box_vol

    num_frac = vfac * tfac
    num_pois = np.random.poisson(num_frac)
    both = np.sum(hs2 * num_pois) * np.ones(nreals)
    return both, np.zeros_like(both), np.zeros_like(both), np.zeros((loudest, nreals))
'''


def _gws_harmonics_at_evo_fobs(fobs_gw, dlnf, evo, harm_range, nreals, box_vol, loudest=5):
    """Calculate GW signal at range of frequency harmonics for a single observer-frame GW frequency.

    Parameters
    ----------
    fobs_gw : float
        Observer-frame GW-frequency in units of [1/sec].  This is a single, float value.
    dlnf : float
        Log-width of observered-frequency bin, i.e. $\\Delta \\ln f$.  This is width of observed
        GW frequency bins.
    evo : `holodeck.evolution.Evolution`
        Initialized and evolved binary evolution instance, storing the binary evolution histories
        of each binary.
    harm_range : list[int]
        Harmonics of the orbital-frequency at which to calculate GW emission.  For circular orbits,
        only [2] is needed, as the GW frequency is twice the orbital frequency.  For eccentric
        orbital, GW emission is produced both at harmonic 1 and higher harmonics.  The higher the
        eccentricity the more GW energy is emitted at higher and higher harmonics.
    nreals : int
        Number of realizations to calculate in Poisson sampling.
    box_vol : float
        Volume of the simulation box that the binary population is derived from.  Units of [cm^3].
    loudest : int
        Number of 'loudest' (highest amplitude) strain values to calculate and return separately.

    Returns
    -------
    mc_ecc_both : (R,) ndarray,
        Combined (background + foreground) GW Strain at this frequency, for `R` realizations.
    mc_ecc_fore : (R,) ndarray,
        GW foreground strain (i.e. loudest single source) at this frequency, for `R` realizations.
    mc_ecc_back : (R,) ndarray,
        GW background strain (i.e. all sources except for the loudest) at this frequency, for `R`
        realizations.
    loud : (L, R) ndarray,
        Strains of the `L` loudest binaries (L=`loudest` input parameter) for each realization.

    """

    # ---- Interpolate data to all harmonics of this frequency
    harm_range = np.asarray(harm_range)
    # (H,) observer-frame orbital-frequency for each harmonic
    fobs_orb = fobs_gw / harm_range
    # Each parameter will be (N, H) = (binaries, harmonics)
    data_harms = evo.at('fobs', fobs_orb, params=_CALC_MC_PARS)

    # Only examine binaries reaching the given locations before redshift zero (other redz=inifinite)
    redz = data_harms['scafa']
    redz = cosmo.a_to_z(redz)
    valid = (redz > 0.0)

    # Broadcast harmonics numbers to correct shape
    harms = np.ones_like(redz, dtype=int) * harm_range[np.newaxis, :]
    # Select only the valid elements, also converts to 1D, i.e. (N, H) ==> (V,)
    harms = harms[valid]
    redz = redz[valid]

    # If there are eccentricities, calculate the freq-dist-function
    eccen = data_harms['eccen']
    if eccen is None:
        gne = 1
    else:
        gne = utils.gw_freq_dist_func(harms, ee=eccen[valid])
        # Select the elements corresponding to the n=2 (circular) harmonic, to use later
        sel_n2 = np.zeros_like(redz, dtype=bool)
        sel_n2[(harms == 2)] = 1
        sel_n2 = sel_n2[valid]

        # BUG: FIX: NOTE: this fails for zero eccentricities (at times?)
        # This is a reasonable, perhaps temporary, fix: when eccentricity is very low, set all
        # harmonics to zero except for n=2
        sel_e0 = (eccen[valid] < 1e-12)
        gne[sel_e0] = 0.0
        gne[sel_n2 & sel_e0] = 1.0

    # Calculate required parameters for valid binaries (V,)
    dcom = cosmo.z_to_dcom(redz)
    frst_orb = utils.frst_from_fobs(fobs_gw, redz) / harms
    mchirp = data_harms['mass'][valid]
    mchirp = utils.chirp_mass(*mchirp.T)
    # Calculate strains from each source
    hs2 = utils.gw_strain_source(mchirp, dcom, frst_orb)**2

    dfdt, _ = utils.dfdt_from_dadt(data_harms['dadt'][valid], data_harms['sepa'][valid], frst_orb=frst_orb)
    _lambda_fact = utils.lambda_factor_dlnf(frst_orb, dfdt, redz, dcom=None) / box_vol
    num_binaries = _lambda_fact * dlnf

    shape = (num_binaries.size, nreals)
    num_pois = np.random.poisson(num_binaries[:, np.newaxis], shape)

    # --- Calculate GW Signals
    temp = hs2 * gne * (2.0 / harms)**2
    both = np.sum(temp[:, np.newaxis] * num_pois / dlnf, axis=0)

    if np.any(num_pois > 0):
        # Find the L loudest binaries in each realizations
        loud = np.sort(temp[:, np.newaxis] * (num_pois > 0), axis=0)[::-1, :]
        fore = loud[0, :]
        loud = loud[:loudest, :]
    else:
        fore = np.zeros_like(both)
        loud = np.zeros((loudest, nreals))

    back = both - fore
    return both, fore, back, loud


def _gws_from_samples(vals, weights, fobs_gw):
    """Calculate GW signals at the given frequencies, from weighted samples of a binary population.

    Parameters
    ----------
    vals : (4, N) ndarray of scalar,
        Arrays of binary parameters.
        * vals[0] : mtot [grams]
        * vals[1] : mrat []
        * vals[2] : redz []
        * vals[3] : *observer*-frame binary *orbital*-frequency [1/sec]
    weights : (N,) array of scalar,
    fobs_gw : (F,) array of scalar,
        Target observer-frame GW-frequencies to calculate GWs at.  Units of [1/sec].

    Returns
    -------
    gff : (F,) ndarry,
        Observer-frame GW-frequencies of the loudest binary in each bin [1/sec].
    gwf : (F,) ndarry,
        GW Foreground: the characteristic strain of the loudest binary in each frequency bin.
    gwb : (F,) ndarry,
        GW Background: the characteristic strain of the GWB in each frequency bin.
        Does not include the strain from the loudest binary in each bin (`gwf`).

    """
    # `fo` is observer-frame GW-frequencies of binary samples
    hs, fo = _strains_from_samples(vals)
    gff, gwf, gwb = gws_from_sampled_strains(fobs_gw, fo, hs, weights)
    return gff, gwf, gwb


def _strains_from_samples(vals):
    """From a sampled binary population, calculate the GW strains.

    Parameters
    ----------
    vals : (4,) array_like of array_like,
        Each element of `vals` is an array of binary parameters, the elements must be:
        * 0) total binary mass [grams]
        * 1) binary mass-ratio [],
        * 2) redshift at this frequency [],
        * 3) *observer*-frame binary *orbital*-frequency [1/sec].

    Returns
    -------
    hs : (N,) ndarray,
        Source strains (i.e. not characteristic strains) of each binary.
    fobs_gw : (N,) ndarray,
        Observer-frame GW-frequencies of each sampled binary.  [1/sec].

    """

    mc = utils.chirp_mass(*utils.m1m2_from_mtmr(vals[0], vals[1]))

    rz = vals[2]
    dc = cosmo.comoving_distance(rz).cgs.value

    fobs_orb = vals[3]
    frst_orb = utils.frst_from_fobs(fobs_orb, rz)
    hs = utils.gw_strain_source(mc, dc, frst_orb)

    fobs_gw = fobs_orb * 2.0
    return hs, fobs_gw


@numba.njit
def gws_from_sampled_strains(fobs_gw_bins, fo, hs, weights):
    """Calculate GW background/foreground from sampled GW strains.

    Parameters
    ----------
    fobs_gw_bins : (F,) array_like of scalar
        Observer-frame GW-frequency bins.
    fo : (S,) array_like of scalar
        Observer-frame GW-frequency of each binary sample.  Units of [1/sec]
    hs : (S,) array_like of scalar
        GW source strain (*not characteristic strain*) of each binary sample.
    weights : (S,) array_like of int
        Weighting factor for each binary.
        NOTE: the GW calculation is ill-defined if weights have fractional values
        (i.e. float values, instead of integral values; but the type itself doesn't matter)

    Returns
    -------
    gwf_freqs : (F,) ndarray of scalar
        Observer-frame GW frequency of foreground sources in each frequency bin.  Units of [1/sec].
    gwfore : (F,) ndarray of scalar
        Strain amplitude of foreground sources in each frequency bin.
    gwback : (F,) ndarray of scalar
        Strain amplitude of the background in each frequency bin.

    """

    # ---- Initialize
    num_samp = fo.size                 # number of binaries/samples
    num_freq = fobs_gw_bins.size - 1           # number of frequency bins (edges - 1)
    gwback = np.zeros(num_freq)        # store GWB characteristic strain
    gwfore = np.zeros(num_freq)        # store loudest binary characteristic strain, for each bin
    gwf_freqs = np.zeros(num_freq)     # store frequency of loudest binary, for each bin

    # ---- Sort input by frequency for faster iteration
    idx = np.argsort(fo)
    fo = fo[idx]
    hs = hs[idx]
    weights = weights[idx]

    # ---- Calculate GW background and foreground in each frequency bin
    ii = 0
    lo = fobs_gw_bins[ii]
    for ff in range(num_freq):
        # upper-bound to this frequency bin
        hi = fobs_gw_bins[ff+1]
        # number of GW cycles (1/dlnf), for conversion to characteristic strain
        dlnf = (np.log(hi) - np.log(lo))
        # amplitude and frequency of the loudest source in this bin
        hmax = 0.0
        fmax = 0.0
        # iterate over all sources with frequencies below this bin's limit (right edge)
        while (fo[ii] < hi) and (ii < num_samp):
            # Store the amplitude and frequency of loudest source
            #    NOTE: loudest source could be a single-sample (weight==1) or from a weighted-bin (weight > 1)
            #          the max
            if (weights[ii] >= 1) and (hs[ii] > hmax):
                hmax = hs[ii]
                fmax = fo[ii]
            # if (weights[ii] > 1.0) and poisson:
            #     h2temp *= np.random.poisson(weights[ii])
            h2temp = weights[ii] * (hs[ii] ** 2)
            gwback[ff] += h2temp

            # increment binary/sample index
            ii += 1

        # subtract foreground source from background
        gwf_freqs[ff] = fmax
        gwback[ff] -= hmax**2
        # Convert to *characteristic* strain
        gwback[ff] = gwback[ff] / dlnf      # hs^2 ==> hc^2  (squared, so dlnf^-1)
        gwfore[ff] = hmax / np.sqrt(dlnf)   # hs ==> hc (not squared, so sqrt of 1/dlnf)
        lo = hi

    gwback = np.sqrt(gwback)
    return gwf_freqs, gwfore, gwback


def sampled_gws_from_sam(sam, fobs_gw, hard=holo.evolution.Hard_GW, **kwargs):
    """Sample the given binary population between the target frequencies, and calculate GW signals.

    NOTE: the input `fobs` are interpretted as bin edges, and GW signals are calculate within the
    corresponding bins.

    Parameters
    ----------
    sam : `Semi_Analytic_Model` instance,
        Binary population to sample.
    fobs_gw : (F+1,) array_like,
        Target observer-frame GW-frequencies of interest in units of [1/sec]
    hard : `holodeck.evolution._Hardening` instance,
        Binary hardening model used to calculate binary residence time at each frequency.
    kwargs : dict,
        Additional keyword-arguments passed to `sample_sam_with_hardening()`

    Returns
    -------
    gff : (F,) ndarry,
        Observer-frame GW-frequencies of the loudest binary in each bin [1/sec].
    gwf : (F,) ndarry,
        GW Foreground: the characteristic strain of the loudest binary in each frequency bin.
    gwb : (F,) ndarry,
        GW Background: the characteristic strain of the GWB in each frequency bin.
        Does not include the strain from the loudest binary in each bin (`gwf`).

    """
    fobs_orb = fobs_gw / 2.0
    vals, weights, edges, dens, mass = holo.sam.sample_sam_with_hardening(sam, hard, fobs=fobs_orb, **kwargs)
    gff, gwf, gwb = _gws_from_samples(vals, weights, fobs_gw)
    return gff, gwf, gwb


def _gws_from_number_grid_centroids(edges, dnum, number, realize):
    """Calculate GWs based on a grid of number-of-binaries.

    # ! BUG: THIS ASSUMES THAT FREQUENCIES ARE NYQUIST SAMPLED !
    # ! otherwise the conversion from hs to hc doesnt work !

    NOTE: `_gws_from_number_grid_integrated()` should be more accurate, but this method better
    matches GWB from sampled (`kale.sample_`) populations!!

    The input number of binaries is `N` s.t. $$N = (d^4 N / [dlog10(M) dq dz dlogf] ) * dlog10(M) dq dz dlogf$$
    The number `N` is evaluated on a 4d grid, specified by `edges`, i.e. $$N = N(M, q, z, f_r)$$
    NOTE: the provided `number` must also summed/integrated over dlogf.
    To calculate characteristic strain, this function divides again by the dlogf term.

    Parameters
    ----------
    edges : (4,) iterable of array_like,
        The edges of each dimension of the parameter space.
        The edges should be, in order: [mtot, mrat, redz, fobs],
        In units of [grams], [], [], [1/sec].
    dnum : (M, Q, Z, F) ndarray,
        Differential comoving number-density of binaries in each bin.
    number : (M, Q, Z, F) ndarray,
        Volumetric comoving number-density of binaries in each bin.
    realize : bool or int,
        Whether or not to calculate one or multiple realizations of the population.
        BUG: explain more.

    Returns
    -------
    hc : (M',Q',Z',F) ndarray,
        Total characteristic GW strain from each bin of parameter space.
        NOTE: to get total strain from all bins, must sum in quarature!
        e.g. ``gwb = np.sqrt(np.square(hc).sum())``

    """

    # # ---- find 'center-of-mass' of each bin (i.e. based on grid edges)
    # # (3, M', Q', Z')
    # # coms = self.grid
    # # ===> (3, M', Q', Z', 1)
    # coms = [cc[..., np.newaxis] for cc in grid]
    # # ===> (4, M', Q', Z', F)
    # coms = np.broadcast_arrays(*coms, fobs[np.newaxis, np.newaxis, np.newaxis, :])

    # # ---- find weighted bin centers
    # # get unweighted centers
    # cent = kale.utils.midpoints(dnum, log=False, axis=(0, 1, 2, 3))
    # # get weighted centers for each dimension
    # for ii, cc in enumerate(coms):
    #     coms[ii] = kale.utils.midpoints(dnum * cc, log=False, axis=(0, 1, 2, 3)) / cent
    # print(f"{kale.utils.jshape(edges)=}, {dnum.shape=}")
    coms = kale.utils.centroids(edges, dnum)

    # ---- calculate GW strain at bin centroids
    mc = utils.chirp_mass(*utils.m1m2_from_mtmr(coms[0], coms[1]))
    dc = cosmo.comoving_distance(coms[2]).cgs.value

    # ! -- 2022-08-19: `edges` should already be using *orbital*-frequency
    fr = utils.frst_from_fobs(coms[3], coms[2])
    # ! old:
    # convert from GW frequency to orbital frequency (divide by 2.0)
    # hs = utils.gw_strain_source(mc, dc, fr/2.0)
    # ! new:
    hs = utils.gw_strain_source(mc, dc, fr)
    # ! --

    # NOTE: for `dlogf` it doesnt matter if these are orbital- or GW- frequencies
    dlogf = np.diff(np.log(edges[-1]))
    dlogf = dlogf[np.newaxis, np.newaxis, np.newaxis, :]

    if realize is True:
        number = np.random.poisson(number)
    elif realize in [None, False]:
        pass
    elif utils.isinteger(realize):
        shape = number.shape + (realize,)
        number = np.random.poisson(number[..., np.newaxis], size=shape)
        hs = hs[..., np.newaxis]
        dlogf = dlogf[..., np.newaxis]
    else:
        err = "`realize` ({}) must be one of {{True, False, integer}}!".format(realize)
        raise ValueError(err)

    number = number / dlogf
    hs = np.nan_to_num(hs)
    hc = number * np.square(hs)

    # # (M',Q',Z',F) ==> (F,)
    # if integrate:
    #     hc = np.sqrt(np.sum(hc, axis=(0, 1, 2)))

    hc = np.sqrt(hc)

    return hc


def _gws_from_number_grid_integrated(edges, dnum, number, realize, integrate=True):
    """

    # ! BUG: THIS ASSUMES THAT FREQUENCIES ARE NYQUIST SAMPLED !
    # ! otherwise the conversion from hs to hc doesnt work !

    """
    grid = np.meshgrid(*edges, indexing='ij')

    # ---- calculate GW strain at bin centroids
    mc = utils.chirp_mass(*utils.m1m2_from_mtmr(grid[0], grid[1]))
    dc = cosmo.comoving_distance(grid[2]).cgs.value
    # These should be *orbital*-frequencies
    fr = utils.frst_from_fobs(grid[3], grid[2])
    hs = utils.gw_strain_source(mc, dc, fr)

    dlnf = np.diff(np.log(edges[-1]))[np.newaxis, np.newaxis, np.newaxis, :]
    integrand = dnum * (hs ** 2)
    hc = utils._integrate_grid_differential_number(edges, integrand, freq=True)
    hc = hc / dlnf

    if realize is True:
        number = np.random.poisson(number) / number
    elif realize in [None, False]:
        pass
    elif utils.isinteger(realize):
        shape = number.shape + (realize,)
        number = np.random.poisson(number[..., np.newaxis], size=shape) / number[..., np.newaxis]
        hc = hc[..., np.newaxis] * np.nan_to_num(number)
    else:
        err = "`realize` ({}) must be one of {{True, False, integer}}!".format(realize)
        raise ValueError(err)

    # (M',Q',Z',F) ==> (F,)
    if integrate:
        hc = np.sqrt(np.sum(hc, axis=(0, 1, 2)))

    return hc


# ==============================================================================
# ====    Deprecated Functions    ====
# ==============================================================================


@utils.deprecated_fail(_gws_harmonics_at_evo_fobs)
def _calc_mc_at_fobs(*args, **kwargs):
    return
