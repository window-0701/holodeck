"""
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
import shutil

import numpy as np
import scipy as sp
import scipy.stats

import holodeck as holo
from holodeck import cosmo
from holodeck.constants import YR
import holodeck.librarian
from holodeck.librarian import lib_utils

MAX_FAILURES = 5

FILES_COPY_TO_OUTPUT = [__file__, holo.librarian.__file__, holo.param_spaces.__file__]


def main():

    try:
        from mpi4py import MPI
        comm = MPI.COMM_WORLD
    except Exception as err:
        comm = None
        holo.log.error(f"failed to load `mpi4py` in {__file__}: {err}")
        holo.log.error("`mpi4py` may not be included in the standard `requirements.txt` file")
        holo.log.error("Check if you have `mpi4py` installed, and if not, please install it")
        raise err

    if comm.rank == 0:
        args = _setup_argparse(comm)
    else:
        args = None

    # share `args` to all processes
    args = comm.bcast(args, root=0)

    # setup log instance, separate for all processes
    log = _setup_log(comm, args)
    args.log = log

    if comm.rank == 0:
        copy_files = FILES_COPY_TO_OUTPUT
        # copy certain files to output directory
        if (not args.resume) and (copy_files is not None):
            for fname in copy_files:
                src_file = Path(fname)
                dst_file = args.output.joinpath("runtime_" + src_file.name)
                shutil.copyfile(src_file, dst_file)
                log.info(f"Copied {fname} to {dst_file}")

        # get parameter-space class
        try:
            # `param_space` attribute must match the name of one of the classes in `holo.param_spaces`
            space_class = getattr(holo.param_spaces, args.param_space)
        except Exception as err:
            log.exception(f"Failed to load '{args.param_space}' from holo.param_spaces!")
            log.exception(err)
            raise err

        # instantiate the parameter space class
        if args.resume:
            # Load pspace object from previous save
            log.info(f"{args.resume=} attempting to load pspace {space_class=} from {args.output=}")
            space, space_fname = holo.librarian.load_pspace_from_dir(log, args.output, space_class)
            log.warning(f"resume={args.resume} :: Loaded param-space save from {space_fname}")
        else:
            space = space_class(log, args.nsamples, args.sam_shape, args.seed)
    else:
        space = None

    # share parameter space across processes
    space = comm.bcast(space, root=0)

    log.info(
        f"param_space={args.param_space}, samples={args.nsamples}, sam_shape={args.sam_shape}, nreals={args.nreals}\n"
        f"nfreqs={args.nfreqs}, pta_dur={args.pta_dur} [yr]\n"
    )
    comm.barrier()

    # Split and distribute index numbers to all processes
    if comm.rank == 0:
        npars = args.nsamples
        indices = range(npars)
        indices = np.random.permutation(indices)
        indices = np.array_split(indices, comm.size)
        num_ind_per_proc = [len(ii) for ii in indices]
        log.info(f"{npars=} cores={comm.size} || max runs per core = {np.max(num_ind_per_proc)}")
    else:
        indices = None

    indices = comm.scatter(indices, root=0)

    iterator = holo.utils.tqdm(indices) if (comm.rank == 0) else np.atleast_1d(indices)

    if (comm.rank == 0) and (not args.resume):
        space_fname = space.save(args.output)
        log.info(f"saved parameter space {space} to {space_fname}")

    comm.barrier()
    beg = datetime.now()
    log.info(f"beginning tasks at {beg}")
    failures = 0

    for par_num in iterator:
        log.info(f"{comm.rank=} {par_num=}")
        pdict = space.param_dict(par_num)
        msg = "\n"
        for kk, vv in pdict.items():
            msg += f"{kk}={vv}\n"
        log.info(msg)

        rv = run_sam_at_pspace_num(args, space, par_num)
        if rv is False:
            failures += 1

        if failures > MAX_FAILURES:
            err = f"Failed {failures} times on rank:{comm.rank}!"
            log.exception(err)
            raise RuntimeError(err)

    end = datetime.now()
    dur = (end - beg)
    log.info(f"\t{comm.rank} done at {str(end)} after {str(dur)} = {dur.total_seconds()}")

    # Make sure all processes are done so that all files are ready for merging
    comm.barrier()

    if (comm.rank == 0):
        log.info("Concatenating outputs into single file")
        holo.librarian.sam_lib_combine(args.output, log)
        log.info("Concatenating completed")


def run_sam_at_pspace_num(args, space, pnum):
    """Run strain calculations for sample-parameter `pnum` in the `space` parameter-space.

    Arguments
    ---------
    args : `argparse.ArgumentParser` instance
        Arguments from the `gen_lib_sams.py` script.
        NOTE: this should be improved.
    space : _Param_Space instance
        Parameter space from which to load `sam` and `hard` instances.
    pnum : int
        Which parameter-sample from `space` should be run.

    Returns
    -------
    rv : bool
        True if this simulation was successfully run.

    """
    log = args.log

    # ---- get output filename for this simulation, check if already exists

    sim_fname = lib_utils._get_sim_fname(args.output_sims, pnum)

    beg = datetime.now()
    log.info(f"{pnum=} :: {sim_fname=} beginning at {beg}")

    if sim_fname.exists():
        log.info(f"File {sim_fname} already exists.  {args.recreate=}")
        # skip existing files unless we specifically want to recreate them
        if not args.recreate:
            return True

    # ---- Setup PTA frequencies
    fobs_cents, fobs_edges = get_freqs(args)
    log.info(f"Created {fobs_cents.size} frequency bins")
    log.info(f"\t[{fobs_cents[0]*YR}, {fobs_cents[-1]*YR}] [1/yr]")
    log.info(f"\t[{fobs_cents[0]*1e9}, {fobs_cents[-1]*1e9}] [nHz]")
    lib_utils.log_mem_usage(log)
    assert args.nfreqs == fobs_cents.size

    # ---- Calculate hc_ss, hc_bg, sspar, and bgpar from SAM

    try:
        log.debug("Selecting `sam` and `hard` instances")
        sam, hard = space(pnum)
        lib_utils.log_mem_usage(log)

        log.debug("Calculating 'edges' and 'number' for this SAM.")
        fobs_orb_edges = fobs_edges / 2.0
        fobs_orb_cents = fobs_cents / 2.0
        data = dict(fobs=fobs_cents, fobs_edges=fobs_edges)

        if not isinstance(hard, (holo.hardening.Fixed_Time_2PL_SAM, holo.hardening.Hard_GW)):
            err = f"`holo.hardening.Fixed_Time_2PL_SAM` must be used here!  Not {hard}!"
            log.exception(err)
            raise RuntimeError(err)

        redz_final, diff_num = sam_cyutils.dynamic_binary_number_at_fobs(
            fobs_orb_cents, sam, hard, cosmo
        )
        edges = [sam.mtot, sam.mrat, sam.redz, fobs_orb_edges]
        number = sam_cyutils.integrate_differential_number_3dx1d(edges, diff_num)

        lib_utils.log_mem_usage(log)

        # if use_redz is None:
        #     try:
        #         use_redz = sam._redz_final
        #         log.info("using `redz_final`")
        #     except AttributeError:
        #         use_redz = sam._redz_prime[:, :, :, np.newaxis] * np.ones_like(number)
        #         log.warning("using `redz_prime`")

        # ---- Calculate SS/CW Sources & binary parameters

        if args.ss_flag:
            log.debug(f"Calculating `ss_gws` for shape ({fobs_cents.size}, {args.nreals}) | {args.params_flag=}")
            vals = holo.single_sources.ss_gws_redz(
                edges, redz_final, number, realize=args.nreals,
                loudest=args.nloudest, params=args.params_flag,
            )
            if args.params_flag:
                hc_ss, hc_bg, sspar, bgpar = vals
                data['sspar'] = sspar
                data['bgpar'] = bgpar
            else:
                hc_ss, hc_bg = vals

            data['hc_ss'] = hc_ss
            data['hc_bg'] = hc_bg
            log.debug(f"{holo.utils.stats(hc_ss)=}")
            log.debug(f"{holo.utils.stats(hc_bg)=}")
            lib_utils.log_mem_usage(log)

        # ---- Calculate GWB

        if args.gwb_flag:
            log.debug(f"Calculating `gwb` for shape ({fobs_cents.size}, {args.nreals})")
            gwb = holo.gravwaves._gws_from_number_grid_integrated_redz(edges, redz_final, number, args.nreals)
            log.debug(f"{holo.utils.stats(gwb)=}")
            lib_utils.log_mem_usage(log)
            data['gwb'] = gwb

        rv = True
    except Exception as err:
        log.exception(f"`run_ss` FAILED on {pnum=}\n")
        log.exception(err)
        rv = False
        data = dict(fail=str(err))

    # ---- Save data to file

    log.debug(f"Saving {pnum} to file | {args.gwb_flag=} {args.ss_flag=} {args.params_flag=}")
    log.debug(f"data has keys: {list(data.keys())}")
    np.savez(sim_fname, **data)
    log.info(f"Saved to {sim_fname}, size {holo.utils.get_file_size(sim_fname)} after {(datetime.now()-beg)}")

    # ---- Plot hc and pars

    if rv and args.plot:
        log.info("generating characteristic strain/psd plots")
        try:
            log.info("generating strain plots")
            plot_fname = args.output_plots.joinpath(sim_fname.name)
            hc_fname = str(plot_fname.with_suffix(''))+"_strain.png"
            fig = holo.plot.plot_bg_ss(fobs_cents, bg=hc_bg, ss=hc_ss)
            fig.savefig(hc_fname, dpi=100)
            # log.info("generating PSD plots")
            # psd_fname = str(plot_fname.with_suffix('')) + "_psd.png"
            # fig = make_ss_plot(fobs_cents, hc_ss, hc_bg, fit_data)
            # fig.savefig(psd_fname, dpi=100)
            # log.info(f"Saved to {psd_fname}, size {holo.utils.get_file_size(psd_fname)}")
            log.info("generating pars plots")
            pars_fname = str(plot_fname.with_suffix('')) + "_pars.png"
            fig = make_pars_plot(fobs_cents, hc_ss, hc_bg, sspar, bgpar)
            fig.savefig(pars_fname, dpi=100)
            log.info(f"Saved to {pars_fname}, size {holo.utils.get_file_size(pars_fname)}")
            plt.close('all')
        except Exception as err:
            log.exception("Failed to make strain plot!")
            log.exception(err)

    return rv


def run_model(sam, hard, nreals, nfreqs, nloudest=5,
              gwb_flag=True, details_flag=False, singles_flag=False, params_flag=False):
    """Run the given modeling, storing requested data
    """
    fobs_cents, fobs_edges = holo.librarian.get_freqs(None)
    if nfreqs is not None:
        fobs_edges = fobs_edges[:nfreqs+1]
        fobs_cents = fobs_cents[:nfreqs]
    fobs_orb_cents = fobs_cents / 2.0     # convert from GW to orbital frequencies
    fobs_orb_edges = fobs_edges / 2.0     # convert from GW to orbital frequencies

    data = dict(fobs_cents=fobs_cents, fobs_edges=fobs_edges)

    redz_final, diff_num = sam_cyutils.dynamic_binary_number_at_fobs(
        fobs_orb_cents, sam, hard, cosmo
    )
    use_redz = redz_final
    edges = [sam.mtot, sam.mrat, sam.redz, fobs_orb_edges]
    number = sam_cyutils.integrate_differential_number_3dx1d(edges, diff_num)
    if details_flag:
        data['static_binary_density'] = sam.static_binary_density
        data['number'] = number
        data['redz_final'] = redz_final
        data['coalescing'] = (redz_final > 0.0)

        gwb_pars, num_pars, gwb_mtot_redz_final, num_mtot_redz_final = _calc_model_details(edges, redz_final, number)

        data['gwb_params'] = gwb_pars
        data['num_params'] = num_pars
        data['gwb_mtot_redz_final'] = gwb_mtot_redz_final
        data['num_mtot_redz_final'] = num_mtot_redz_final

    # calculate single sources and/or binary parameters
    if singles_flag or params_flag:
        nloudest = nloudest if singles_flag else 1

        vals = holo.single_sources.ss_gws_redz(
            edges, use_redz, number, realize=nreals,
            loudest=nloudest, params=params_flag,
        )
        if params_flag:
            hc_ss, hc_bg, sspar, bgpar = vals
            data['sspar'] = sspar
            data['bgpar'] = bgpar
        else:
            hc_ss, hc_bg = vals

        if singles_flag:
            data['hc_ss'] = hc_ss
            data['hc_bg'] = hc_bg

    if gwb_flag:
        gwb = holo.gravwaves._gws_from_number_grid_integrated_redz(edges, use_redz, number, nreals)
        data['gwb'] = gwb

    return data


def _calc_model_details(edges, redz_final, number):
    """

    Parameters
    ----------
    edges : (4,) list of 1darrays
        [mtot, mrat, redz, fobs_orb_edges] with shapes (M, Q, Z, F+1)
    redz_final : (M,Q,Z,F)
        Redshift final (redshift at the given frequencies).
    number : (M-1, Q-1, Z-1, F)
        Absolute number of binaries in the given bin (dimensionless).

    """

    redz = edges[2]
    nmbins = len(edges[0]) - 1
    nzbins = len(redz) - 1
    nfreqs = len(edges[3]) - 1
    # (M-1, Q-1, Z-1, F) characteristic-strain squared for each bin
    hc2 = holo.gravwaves.char_strain_sq_from_bin_edges_redz(edges, redz_final)
    # strain-squared weighted number of binaries
    hc2_num = hc2 * number
    # (F,) total GWB in each frequency bin
    denom = np.sum(hc2_num, axis=(0, 1, 2))
    gwb_pars = []
    num_pars = []

    # Iterate over the parameters to calculate weighted averaged of [mtot, mrat, redz]
    for ii in range(3):
        # Get the indices of the dimensions that we will be marginalizing (summing) over
        # we'll also keep things in terms of redshift and frequency bins, so at most we marginalize
        # over 0-mtot and 1-mrat
        margins = [0, 1]
        # if we're targeting mtot or mrat, then don't marginalize over that parameter
        if ii in margins:
            del margins[ii]
        margins = tuple(margins)

        # Get straight-squared weighted values (numerator, of the average)
        numer = np.sum(hc2_num, axis=margins)
        # divide by denominator to get average
        tpar = numer / denom
        gwb_pars.append(tpar)

        # Get the total number of binaries
        tpar = np.sum(number, axis=margins)
        num_pars.append(tpar)

    # ---- calculate redz_final based distributions

    # get final-redshift at bin centers
    rz = redz_final.copy()
    for ii in range(3):
        rz = utils.midpoints(rz, axis=ii)

    gwb_mtot_redz_final = np.zeros((nmbins, nzbins, nfreqs))
    num_mtot_redz_final = np.zeros((nmbins, nzbins, nfreqs))
    gwb_rz = np.zeros((nzbins, nfreqs))
    num_rz = np.zeros((nzbins, nfreqs))
    for ii in range(nfreqs):
        rz_flat = rz[:, :, :, ii].flatten()
        # calculate GWB-weighted average final-redshift
        numer, *_ = sp.stats.binned_statistic(
            rz_flat, hc2_num[:, :, :, ii].flatten(), bins=redz, statistic='sum'
        )
        tpar = numer / denom[ii]
        gwb_rz[:, ii] = tpar

        # calculate average final-redshift (number weighted)
        tpar, *_ = sp.stats.binned_statistic(
            rz_flat, number[:, :, :, ii].flatten(), bins=redz, statistic='sum'
        )
        num_rz[:, ii] = tpar

        # Get values vs. mtot for redz-final
        for mm in range(nmbins):
            rz_flat = rz[mm, :, :, ii].flatten()
            numer, *_ = sp.stats.binned_statistic(
                rz_flat, hc2_num[mm, :, :, ii].flatten(), bins=redz, statistic='sum'
            )
            tpar = numer / denom[ii]
            gwb_mtot_redz_final[mm, :, ii] = tpar

            tpar, *_ = sp.stats.binned_statistic(
                rz_flat, number[mm, :, :, ii].flatten(), bins=redz, statistic='sum'
            )
            num_mtot_redz_final[mm, :, ii] = tpar

    gwb_pars.append(gwb_rz)
    num_pars.append(num_rz)

    return gwb_pars, num_pars, gwb_mtot_redz_final, num_mtot_redz_final


def _setup_argparse(comm, *args, **kwargs):
    assert comm.rank == 0

    parser = argparse.ArgumentParser()
    parser.add_argument('param_space', type=str,
                        help="Parameter space class name, found in 'holodeck.param_spaces'.")

    parser.add_argument('output', metavar='output', type=str,
                        help='output path [created if doesnt exist]')

    parser.add_argument('-n', '--nsamples', action='store', dest='nsamples', type=int, default=1000,
                        help='number of parameter space samples')
    parser.add_argument('-r', '--nreals', action='store', dest='nreals', type=int,
                        help='number of realiz  ations', default=holo.librarian.DEF_NUM_REALS)
    parser.add_argument('-d', '--dur', action='store', dest='pta_dur', type=float,
                        help='PTA observing duration [yrs]', default=holo.librarian.DEF_PTA_DUR)
    parser.add_argument('-f', '--nfreqs', action='store', dest='nfreqs', type=int,
                        help='Number of frequency bins', default=holo.librarian.DEF_NUM_FBINS)
    parser.add_argument('-s', '--shape', action='store', dest='sam_shape', type=int,
                        help='Shape of SAM grid', default=None)
    parser.add_argument('-l', '--nloudest', action='store', dest='nloudest', type=int,
                        help='Number of loudest single sources', default=holo.librarian.DEF_NUM_LOUDEST)
    parser.add_argument('--gwb', action='store_true', dest="gwb_flag", default=False,
                        help="calculate and store the 'gwb' per se")
    parser.add_argument('--ss', action='store_true', dest="ss_flag", default=False,
                        help="calculate and store SS/CW sources and the BG separately")
    parser.add_argument('--params', action='store_true', dest="params_flag", default=False,
                        help="calculate and store SS/BG binary parameters [NOTE: requires `--ss`]")

    parser.add_argument('--resume', action='store_true', default=False,
                        help='resume production of a library by loading previous parameter-space from output directory')
    parser.add_argument('--recreate', action='store_true', default=False,
                        help='recreating existing simulation files')
    parser.add_argument('--plot', action='store_true', default=False,
                        help='produce plots for each simulation configuration')
    parser.add_argument('--seed', action='store', type=int, default=None,
                        help='Random seed to use')

    # parser.add_argument('-v', '--verbose', action='store_true', default=False, dest='verbose',
    #                     help='verbose output [INFO]')

    args = parser.parse_args(*args, **kwargs)

    output = Path(args.output).resolve()
    if not output.is_absolute:
        output = Path('.').resolve() / output
        output = output.resolve()

    if not args.gwb_flag and not args.ss_flag:
        raise RuntimeError("Either `--gwb` or `--ss` is required!")

    if args.params_flag and not args.ss_flag:
        raise RuntimeError("`--params` requires the `--ss` option!")

    if args.resume:
        if not output.exists() or not output.is_dir():
            raise FileNotFoundError(f"`--resume` is active but output path does not exist! '{output}'")

    # ---- Create output directories as needed

    output.mkdir(parents=True, exist_ok=True)
    holo.utils.mpi_print(f"output path: {output}")
    args.output = output

    output_sims = output.joinpath("sims")
    output_sims.mkdir(parents=True, exist_ok=True)
    args.output_sims = output_sims

    output_logs = output.joinpath("logs")
    output_logs.mkdir(parents=True, exist_ok=True)
    args.output_logs = output_logs

    if args.plot:
        output_plots = output.joinpath("figs")
        output_plots.mkdir(parents=True, exist_ok=True)
        args.output_plots = output_plots

    return args


def _setup_log(comm, args):
    beg = datetime.now()
    log_name = f"holodeck__gen_lib_sams_{beg.strftime('%Y%m%d-%H%M%S')}"
    if comm.rank > 0:
        log_name = f"_{log_name}_r{comm.rank}"

    output = args.output_logs
    fname = f"{output.joinpath(log_name)}.log"
    # log_lvl = holo.logger.INFO if args.verbose else holo.logger.WARNING
    log_lvl = holo.logger.DEBUG
    tostr = sys.stdout if comm.rank == 0 else False
    log = holo.logger.get_logger(name=log_name, level_stream=log_lvl, tofile=fname, tostr=tostr)
    log.info(f"Output path: {output}")
    log.info(f"        log: {fname}")
    log.info(args)
    return log


def get_freqs(args):
    """Get PTA frequency bin centers and edges.

    Arguments
    ---------
    args : `argparse` namespace, with `pta_dur` and `nfreqs` attributes.
        `pta_dur` must be in units of [yr]

    Returns
    -------
    fobs_cents : (F,) ndarray
        Observer-frame GW-frequencies at frequency-bin centers.
    fobs_edges : (F+1,) ndarray
        Observer-frame GW-frequencies at frequency-bin edges.

    """
    pta_dur = args.pta_dur * YR
    nfreqs = args.nfreqs
    fobs_cents, fobs_edges = holo.utils.pta_freqs(dur=pta_dur, num=nfreqs)
    return fobs_cents, fobs_edges


if __name__ == "__main__":
    main()