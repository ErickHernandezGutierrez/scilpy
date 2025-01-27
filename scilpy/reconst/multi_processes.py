import itertools
import logging
import multiprocessing

from dipy.direction.peaks import peak_directions
from dipy.reconst.multi_voxel import MultiVoxelFit
from dipy.reconst.odf import gfa
from dipy.reconst.shm import sh_to_sf_matrix, order_from_ncoef
from dipy.reconst.mcsd import MSDeconvFit
import numpy as np

from scilpy.reconst.divide_fit import gamma_data2fit

from dipy.utils.optpkg import optional_package
cvx, have_cvxpy, _ = optional_package("cvxpy")


def fit_from_model_parallel(args):
    model = args[0]
    data = args[1]
    chunk_id = args[2]

    sub_fit_array = np.zeros((data.shape[0],), dtype='object')
    for i in range(data.shape[0]):
        if data[i].any():
            try:
                sub_fit_array[i] = model.fit(data[i])
            except cvx.error.SolverError:
                coeff = np.full((len(model.n)), np.NaN)
                sub_fit_array[i] = MSDeconvFit(model, coeff, None)

    return chunk_id, sub_fit_array


def fit_from_model(model, data, mask=None, nbr_processes=None):
    """Fit the model to data

    Parameters
    ----------
    model : a model instance
        `model` will be used to fit the data.
    data : np.ndarray (4d)
        Diffusion data.
    mask : np.ndarray, optional
        If `mask` is provided, only the data inside the mask will be
        used for computations.
    nbr_processes : int, optional
        The number of subprocesses to use.
        Default: multiprocessing.cpu_count()

    Returns
    -------
    fit_array : np.ndarray
        Array containing the fit
    """
    data_shape = data.shape
    if mask is None:
        mask = np.sum(data, axis=3).astype(bool)
    else:
        mask_any = np.sum(data, axis=3).astype(bool)
        mask *= mask_any

    nbr_processes = multiprocessing.cpu_count() \
        if nbr_processes is None or nbr_processes <= 0 \
        else nbr_processes

    # Ravel the first 3 dimensions while keeping the 4th intact, like a list of
    # 1D time series voxels. Then separate it in chunks of len(nbr_processes).
    data = data[mask].reshape((np.count_nonzero(mask), data_shape[3]))
    chunks = np.array_split(data, nbr_processes)

    chunk_len = np.cumsum([0] + [len(c) for c in chunks])
    pool = multiprocessing.Pool(nbr_processes)
    results = pool.map(fit_from_model_parallel,
                       zip(itertools.repeat(model),
                           chunks,
                           np.arange(len(chunks))))
    pool.close()
    pool.join()

    # Re-assemble the chunk together in the original shape.
    fit_array = np.zeros(data_shape[0:3], dtype='object')
    tmp_fit_array = np.zeros((np.count_nonzero(mask)), dtype='object')
    for i, fit in results:
        tmp_fit_array[chunk_len[i]:chunk_len[i+1]] = fit

    fit_array[mask] = tmp_fit_array
    fit_array = MultiVoxelFit(model, fit_array, mask)

    return fit_array


def peaks_from_sh_parallel(args):
    shm_coeff = args[0]
    B = args[1]
    sphere = args[2]
    relative_peak_threshold = args[3]
    absolute_threshold = args[4]
    min_separation_angle = args[5]
    npeaks = args[6]
    normalize_peaks = args[7]
    chunk_id = args[8]
    is_symmetric = args[9]

    data_shape = shm_coeff.shape[0]
    peak_dirs = np.zeros((data_shape, npeaks, 3))
    peak_values = np.zeros((data_shape, npeaks))
    peak_indices = np.zeros((data_shape, npeaks), dtype='int')
    peak_indices.fill(-1)

    for idx in range(len(shm_coeff)):
        if shm_coeff[idx].any():
            odf = np.dot(shm_coeff[idx], B)
            odf[odf < absolute_threshold] = 0.

            dirs, peaks, ind = peak_directions(odf, sphere,
                                               relative_peak_threshold,
                                               min_separation_angle,
                                               is_symmetric)

            if peaks.shape[0] != 0:
                n = min(npeaks, peaks.shape[0])

                peak_dirs[idx][:n] = dirs[:n]
                peak_indices[idx][:n] = ind[:n]
                peak_values[idx][:n] = peaks[:n]

                if normalize_peaks:
                    peak_values[idx][:n] /= peaks[0]
                    peak_dirs[idx] *= peak_values[idx][:, None]

    return chunk_id, peak_dirs, peak_values, peak_indices


def peaks_from_sh(shm_coeff, sphere, mask=None, relative_peak_threshold=0.5,
                  absolute_threshold=0, min_separation_angle=25,
                  normalize_peaks=False, npeaks=5,
                  sh_basis_type='descoteaux07', nbr_processes=None,
                  full_basis=False, is_symmetric=True):
    """Computes peaks from given spherical harmonic coefficients

    Parameters
    ----------
    shm_coeff : np.ndarray
        Spherical harmonic coefficients
    sphere : Sphere
        The Sphere providing discrete directions for evaluation.
    mask : np.ndarray, optional
        If `mask` is provided, only the data inside the mask will be
        used for computations.
    relative_peak_threshold : float, optional
        Only return peaks greater than ``relative_peak_threshold * m`` where m
        is the largest peak.
        Default: 0.5
    absolute_threshold : float, optional
        Absolute threshold on fODF amplitude. This value should be set to
        approximately 1.5 to 2 times the maximum fODF amplitude in isotropic
        voxels (ex. ventricles). `scil_compute_fodf_max_in_ventricles.py`
        can be used to find the maximal value.
        Default: 0
    min_separation_angle : float in [0, 90], optional
        The minimum distance between directions. If two peaks are too close
        only the larger of the two is returned.
        Default: 25
    normalize_peaks : bool, optional
        If true, all peak values are calculated relative to `max(odf)`.
    npeaks : int, optional
        Maximum number of peaks found (default 5 peaks).
    sh_basis_type : str, optional
        Type of spherical harmonic basis used for `shm_coeff`. Either
        `descoteaux07` or `tournier07`.
        Default: `descoteaux07`
    nbr_processes: int, optional
        The number of subprocesses to use.
        Default: multiprocessing.cpu_count()
    full_basis: bool, optional
        If True, SH coefficients are expressed using a full basis.
        Default: False
    is_symmetric: bool, optional
        If False, antipodal sphere directions are considered distinct.
        Default: True

    Returns
    -------
    tuple of np.ndarray
        peak_dirs, peak_values, peak_indices
    """
    sh_order = order_from_ncoef(shm_coeff.shape[-1], full_basis)
    B, _ = sh_to_sf_matrix(sphere, sh_order, sh_basis_type, full_basis)

    data_shape = shm_coeff.shape
    if mask is None:
        mask = np.sum(shm_coeff, axis=3).astype(bool)

    nbr_processes = multiprocessing.cpu_count() if nbr_processes is None \
        or nbr_processes < 0 else nbr_processes

    # Ravel the first 3 dimensions while keeping the 4th intact, like a list of
    # 1D time series voxels. Then separate it in chunks of len(nbr_processes).
    shm_coeff = shm_coeff[mask].reshape(
        (np.count_nonzero(mask), data_shape[3]))
    chunks = np.array_split(shm_coeff, nbr_processes)
    chunk_len = np.cumsum([0] + [len(c) for c in chunks])

    pool = multiprocessing.Pool(nbr_processes)
    results = pool.map(peaks_from_sh_parallel,
                       zip(chunks,
                           itertools.repeat(B),
                           itertools.repeat(sphere),
                           itertools.repeat(relative_peak_threshold),
                           itertools.repeat(absolute_threshold),
                           itertools.repeat(min_separation_angle),
                           itertools.repeat(npeaks),
                           itertools.repeat(normalize_peaks),
                           np.arange(len(chunks)),
                           itertools.repeat(is_symmetric)))
    pool.close()
    pool.join()

    # Re-assemble the chunk together in the original shape.
    peak_dirs_array = np.zeros(data_shape[0:3] + (npeaks, 3))
    peak_values_array = np.zeros(data_shape[0:3] + (npeaks,))
    peak_indices_array = np.zeros(data_shape[0:3] + (npeaks,))

    # tmp arrays are neccesary to avoid inserting data in returned variable
    # rather than the original array
    tmp_peak_dirs_array = np.zeros((np.count_nonzero(mask), npeaks, 3))
    tmp_peak_values_array = np.zeros((np.count_nonzero(mask), npeaks))
    tmp_peak_indices_array = np.zeros((np.count_nonzero(mask), npeaks))
    for i, peak_dirs, peak_values, peak_indices in results:
        tmp_peak_dirs_array[chunk_len[i]:chunk_len[i+1], :, :] = peak_dirs
        tmp_peak_values_array[chunk_len[i]:chunk_len[i+1], :] = peak_values
        tmp_peak_indices_array[chunk_len[i]:chunk_len[i+1], :] = peak_indices

    peak_dirs_array[mask] = tmp_peak_dirs_array
    peak_values_array[mask] = tmp_peak_values_array
    peak_indices_array[mask] = tmp_peak_indices_array

    return peak_dirs_array, peak_values_array, peak_indices_array


def maps_from_sh_parallel(args):
    shm_coeff = args[0]
    _ = args[1]
    peak_values = args[2]
    peak_indices = args[3]
    B = args[4]
    sphere = args[5]
    gfa_thr = args[6]
    chunk_id = args[7]

    data_shape = shm_coeff.shape[0]
    nufo_map = np.zeros(data_shape)
    afd_max = np.zeros(data_shape)
    afd_sum = np.zeros(data_shape)
    rgb_map = np.zeros((data_shape, 3))
    gfa_map = np.zeros(data_shape)
    qa_map = np.zeros((data_shape, peak_values.shape[1]))

    max_odf = 0
    global_max = -np.inf
    for idx in range(len(shm_coeff)):
        if shm_coeff[idx].any():
            odf = np.dot(shm_coeff[idx], B)
            odf = odf.clip(min=0)
            sum_odf = np.sum(odf)
            max_odf = np.maximum(max_odf, sum_odf)
            if sum_odf > 0:
                rgb_map[idx] = np.dot(np.abs(sphere.vertices).T, odf)
                rgb_map[idx] /= np.linalg.norm(rgb_map[idx])
                rgb_map[idx] *= sum_odf
            gfa_map[idx] = gfa(odf)
            if gfa_map[idx] < gfa_thr:
                global_max = max(global_max, odf.max())
            elif np.sum(peak_indices[idx] > -1):
                nufo_map[idx] = np.sum(peak_indices[idx] > -1)
                afd_max[idx] = peak_values[idx].max()
                afd_sum[idx] = np.sqrt(np.dot(shm_coeff[idx], shm_coeff[idx]))
                qa_map = peak_values[idx] - odf.min()
                global_max = max(global_max, peak_values[idx][0])

    return chunk_id, nufo_map, afd_max, afd_sum, rgb_map, \
        gfa_map, qa_map, max_odf, global_max


def maps_from_sh(shm_coeff, peak_dirs, peak_values, peak_indices, sphere,
                 mask=None, gfa_thr=0, sh_basis_type='descoteaux07',
                 nbr_processes=None):
    """Computes maps from given SH coefficients and peaks

    Parameters
    ----------
    shm_coeff : np.ndarray
        Spherical harmonic coefficients
    peak_dirs : np.ndarray
        Peak directions
    peak_values : np.ndarray
        Peak values
    peak_indices : np.ndarray
        Peak indices
    sphere : Sphere
        The Sphere providing discrete directions for evaluation.
    mask : np.ndarray, optional
        If `mask` is provided, only the data inside the mask will be
        used for computations.
    gfa_thr : float, optional
        Voxels with gfa less than `gfa_thr` are skipped for all metrics, except
        `rgb_map`.
        Default: 0
    sh_basis_type : str, optional
        Type of spherical harmonic basis used for `shm_coeff`. Either
        `descoteaux07` or `tournier07`.
        Default: `descoteaux07`
    nbr_processes: int, optional
        The number of subprocesses to use.
        Default: multiprocessing.cpu_count()

    Returns
    -------
    tuple of np.ndarray
        nufo_map, afd_max, afd_sum, rgb_map, gfa, qa
    """
    sh_order = order_from_ncoef(shm_coeff.shape[-1])
    B, _ = sh_to_sf_matrix(sphere, sh_order, sh_basis_type)

    data_shape = shm_coeff.shape
    if mask is None:
        mask = np.sum(shm_coeff, axis=3).astype(bool)

    nbr_processes = multiprocessing.cpu_count() \
        if nbr_processes is None or nbr_processes < 0 \
        else nbr_processes

    npeaks = peak_values.shape[3]
    # Ravel the first 3 dimensions while keeping the 4th intact, like a list of
    # 1D time series voxels. Then separate it in chunks of len(nbr_processes).
    shm_coeff = shm_coeff[mask].reshape(
        (np.count_nonzero(mask), data_shape[3]))
    peak_dirs = peak_dirs[mask].reshape((np.count_nonzero(mask), npeaks, 3))
    peak_values = peak_values[mask].reshape((np.count_nonzero(mask), npeaks))
    peak_indices = peak_indices[mask].reshape((np.count_nonzero(mask), npeaks))
    shm_coeff_chunks = np.array_split(shm_coeff, nbr_processes)
    peak_dirs_chunks = np.array_split(peak_dirs, nbr_processes)
    peak_values_chunks = np.array_split(peak_values, nbr_processes)
    peak_indices_chunks = np.array_split(peak_indices, nbr_processes)
    chunk_len = np.cumsum([0] + [len(c) for c in shm_coeff_chunks])

    pool = multiprocessing.Pool(nbr_processes)
    results = pool.map(maps_from_sh_parallel,
                       zip(shm_coeff_chunks,
                           peak_dirs_chunks,
                           peak_values_chunks,
                           peak_indices_chunks,
                           itertools.repeat(B),
                           itertools.repeat(sphere),
                           itertools.repeat(gfa_thr),
                           np.arange(len(shm_coeff_chunks))))
    pool.close()
    pool.join()

    # Re-assemble the chunk together in the original shape.
    nufo_map_array = np.zeros(data_shape[0:3])
    afd_max_array = np.zeros(data_shape[0:3])
    afd_sum_array = np.zeros(data_shape[0:3])
    rgb_map_array = np.zeros(data_shape[0:3] + (3,))
    gfa_map_array = np.zeros(data_shape[0:3])
    qa_map_array = np.zeros(data_shape[0:3] + (npeaks,))

    # tmp arrays are neccesary to avoid inserting data in returned variable
    # rather than the original array
    tmp_nufo_map_array = np.zeros((np.count_nonzero(mask)))
    tmp_afd_max_array = np.zeros((np.count_nonzero(mask)))
    tmp_afd_sum_array = np.zeros((np.count_nonzero(mask)))
    tmp_rgb_map_array = np.zeros((np.count_nonzero(mask), 3))
    tmp_gfa_map_array = np.zeros((np.count_nonzero(mask)))
    tmp_qa_map_array = np.zeros((np.count_nonzero(mask), npeaks))

    all_time_max_odf = -np.inf
    all_time_global_max = -np.inf
    for (i, nufo_map, afd_max, afd_sum, rgb_map,
         gfa_map, qa_map, max_odf, global_max) in results:
        all_time_max_odf = max(all_time_global_max, max_odf)
        all_time_global_max = max(all_time_global_max, global_max)

        tmp_nufo_map_array[chunk_len[i]:chunk_len[i+1]] = nufo_map
        tmp_afd_max_array[chunk_len[i]:chunk_len[i+1]] = afd_max
        tmp_afd_sum_array[chunk_len[i]:chunk_len[i+1]] = afd_sum
        tmp_rgb_map_array[chunk_len[i]:chunk_len[i+1], :] = rgb_map
        tmp_gfa_map_array[chunk_len[i]:chunk_len[i+1]] = gfa_map
        tmp_qa_map_array[chunk_len[i]:chunk_len[i+1], :] = qa_map

    nufo_map_array[mask] = tmp_nufo_map_array
    afd_max_array[mask] = tmp_afd_max_array
    afd_sum_array[mask] = tmp_afd_sum_array
    rgb_map_array[mask] = tmp_rgb_map_array
    gfa_map_array[mask] = tmp_gfa_map_array
    qa_map_array[mask] = tmp_qa_map_array

    rgb_map_array /= all_time_max_odf
    rgb_map_array *= 255
    qa_map_array /= all_time_global_max

    afd_unique = np.unique(afd_max_array)
    if np.array_equal(np.array([0, 1]), afd_unique) \
            or np.array_equal(np.array([1]), afd_unique):
        logging.warning('All AFD_max values are 1. The peaks seem normalized.')

    return(nufo_map_array, afd_max_array, afd_sum_array,
           rgb_map_array, gfa_map_array, qa_map_array)


def convert_sh_basis_parallel(args):
    sh = args[0]
    B_in = args[1]
    invB_out = args[2]
    chunk_id = args[3]

    for idx in range(sh.shape[0]):
        if sh[idx].any():
            sf = np.dot(sh[idx], B_in)
            sh[idx] = np.dot(sf, invB_out)

    return chunk_id, sh


def convert_sh_basis(shm_coeff, sphere, mask=None,
                     input_basis='descoteaux07', nbr_processes=None,
                     is_input_legacy=True, is_output_legacy=True):
    """Converts spherical harmonic coefficients between two bases

    Parameters
    ----------
    shm_coeff : np.ndarray
        Spherical harmonic coefficients
    sphere : Sphere
        The Sphere providing discrete directions for evaluation.
    mask : np.ndarray, optional
        If `mask` is provided, only the data inside the mask will be
        used for computations.
    input_basis : str, optional
        Type of spherical harmonic basis used for `shm_coeff`. Either
        `descoteaux07` or `tournier07`.
        Default: `descoteaux07`
    nbr_processes: int, optional
        The number of subprocesses to use.
        Default: multiprocessing.cpu_count()
    is_input_legacy: bool, optional
        If true, this means that the input SH used a legacy basis definition
        for backward compatibility with previous ``tournier07`` and
        ``descoteaux07`` implementations.
        Default: True
    is_output_legacy: bool, optional
        If true, this means that the output SH will use a legacy basis
        definition for backward compatibility with previous ``tournier07`` and
        ``descoteaux07`` implementations.
        Default: True

    Returns
    -------
    shm_coeff_array : np.ndarray
        Spherical harmonic coefficients in the desired basis.
    """
    output_basis = 'descoteaux07' \
        if input_basis == 'tournier07' \
        else 'tournier07'

    sh_order = order_from_ncoef(shm_coeff.shape[-1])
    B_in, _ = sh_to_sf_matrix(sphere, sh_order, input_basis,
                              legacy=is_input_legacy)
    _, invB_out = sh_to_sf_matrix(sphere, sh_order, output_basis,
                                  legacy=is_output_legacy)

    data_shape = shm_coeff.shape
    if mask is None:
        mask = np.sum(shm_coeff, axis=3).astype(bool)

    nbr_processes = multiprocessing.cpu_count() \
        if nbr_processes is None or nbr_processes < 0 \
        else nbr_processes

    # Ravel the first 3 dimensions while keeping the 4th intact, like a list of
    # 1D time series voxels. Then separate it in chunks of len(nbr_processes).
    shm_coeff = shm_coeff[mask].reshape(
        (np.count_nonzero(mask), data_shape[3]))
    shm_coeff_chunks = np.array_split(shm_coeff, nbr_processes)
    chunk_len = np.cumsum([0] + [len(c) for c in shm_coeff_chunks])

    pool = multiprocessing.Pool(nbr_processes)
    results = pool.map(convert_sh_basis_parallel,
                       zip(shm_coeff_chunks,
                           itertools.repeat(B_in),
                           itertools.repeat(invB_out),
                           np.arange(len(shm_coeff_chunks))))
    pool.close()
    pool.join()

    # Re-assemble the chunk together in the original shape.
    shm_coeff_array = np.zeros(data_shape)
    tmp_shm_coeff_array = np.zeros((np.count_nonzero(mask), data_shape[3]))
    for i, new_shm_coeff in results:
        tmp_shm_coeff_array[chunk_len[i]:chunk_len[i+1], :] = new_shm_coeff

    shm_coeff_array[mask] = tmp_shm_coeff_array

    return shm_coeff_array


def convert_sh_to_sf_parallel(args):
    sh = args[0]
    B_in = args[1]
    new_output_dim = args[2]
    chunk_id = args[3]
    sf = np.zeros((sh.shape[0], new_output_dim), dtype=np.float32)

    for idx in range(sh.shape[0]):
        if sh[idx].any():
            sf[idx] = np.dot(sh[idx], B_in)

    return chunk_id, sf


def convert_sh_to_sf(shm_coeff, sphere, mask=None, dtype="float32",
                     input_basis='descoteaux07', input_full_basis=False,
                     nbr_processes=multiprocessing.cpu_count()):
    """Converts spherical harmonic coefficients to an SF sphere

    Parameters
    ----------
    shm_coeff : np.ndarray
        Spherical harmonic coefficients
    sphere : Sphere
        The Sphere providing discrete directions for evaluation.
    mask : np.ndarray, optional
        If `mask` is provided, only the data inside the mask will be
        used for computations.
    dtype : str
        Datatype to use for computation and output array.
        Either `float32` or `float64`. Default: `float32`
    input_basis : str, optional
        Type of spherical harmonic basis used for `shm_coeff`. Either
        `descoteaux07` or `tournier07`.
        Default: `descoteaux07`
    input_full_basis : bool
        If True, use a full SH basis (even and odd orders) for the input SH
        coefficients.
    nbr_processes: int, optional
        The number of subprocesses to use.
        Default: multiprocessing.cpu_count()

    Returns
    -------
    shm_coeff_array : np.ndarray
        Spherical harmonic coefficients in the desired basis.
    """
    assert dtype in ["float32", "float64"], "Only `float32` and `float64` " \
                                            "should be used."

    sh_order = order_from_ncoef(shm_coeff.shape[-1],
                                full_basis=input_full_basis)
    B_in, _ = sh_to_sf_matrix(sphere, sh_order, basis_type=input_basis,
                              full_basis=input_full_basis)
    B_in = B_in.astype(dtype)

    data_shape = shm_coeff.shape
    if mask is None:
        mask = np.sum(shm_coeff, axis=3).astype(bool)

    # Ravel the first 3 dimensions while keeping the 4th intact, like a list of
    # 1D time series voxels. Then separate it in chunks of len(nbr_processes).
    shm_coeff = shm_coeff[mask].reshape(
        (np.count_nonzero(mask), data_shape[3]))
    shm_coeff_chunks = np.array_split(shm_coeff, nbr_processes)
    chunk_len = np.cumsum([0] + [len(c) for c in shm_coeff_chunks])

    pool = multiprocessing.Pool(nbr_processes)
    results = pool.map(convert_sh_to_sf_parallel,
                       zip(shm_coeff_chunks,
                           itertools.repeat(B_in),
                           itertools.repeat(len(sphere.vertices)),
                           np.arange(len(shm_coeff_chunks))))
    pool.close()
    pool.join()

    # Re-assemble the chunk together in the original shape.
    new_shape = data_shape[:3] + (len(sphere.vertices),)
    sf_array = np.zeros(new_shape, dtype=dtype)
    tmp_sf_array = np.zeros((np.count_nonzero(mask), new_shape[3]),
                            dtype=dtype)
    for i, new_sf in results:
        tmp_sf_array[chunk_len[i]:chunk_len[i + 1], :] = new_sf

    sf_array[mask] = tmp_sf_array

    return sf_array


def fit_gamma_parallel(args):
    data = args[0]
    gtab_infos = args[1]
    fit_iters = args[2]
    random_iters = args[3]
    do_weight_bvals = args[4]
    do_weight_pa = args[5]
    do_multiple_s0 = args[6]
    chunk_id = args[7]

    sub_fit_array = np.zeros((data.shape[0], 4))
    for i in range(data.shape[0]):
        if data[i].any():
            sub_fit_array[i] = gamma_data2fit(data[i], gtab_infos, fit_iters,
                                              random_iters, do_weight_bvals,
                                              do_weight_pa, do_multiple_s0)

    return chunk_id, sub_fit_array


def fit_gamma(data, gtab_infos, mask=None, fit_iters=1, random_iters=50,
              do_weight_bvals=False, do_weight_pa=False, do_multiple_s0=False,
              nbr_processes=None):
    """Fit the gamma model to data

    Parameters
    ----------
    data : np.ndarray (4d)
        Diffusion data, powder averaged. Obtained as output of the function
        `reconst.b_tensor_utils.generate_powder_averaged_data`.
    gtab_infos : np.ndarray
        Contains information about the gtab, such as the unique bvals, the
        encoding types, the number of directions and the acquisition index.
        Obtained as output of the function
        `reconst.b_tensor_utils.generate_powder_averaged_data`.
    mask : np.ndarray, optional
        If `mask` is provided, only the data inside the mask will be
        used for computations.
    fit_iters : int, optional
        Number of iterations in the gamma fit. Defaults to 1.
    random_iters : int, optional
        Number of random sets of parameters tested to find the initial
        parameters. Defaults to 50.
    do_weight_bvals : bool , optional
        If set, does a weighting on the bvalues in the gamma fit.
    do_weight_pa : bool, optional
        If set, does a powder averaging weighting in the gamma fit.
    do_multiple_s0 : bool, optional
        If set, takes into account multiple baseline signals.
    nbr_processes : int, optional
        The number of subprocesses to use.
        Default: multiprocessing.cpu_count()

    Returns
    -------
    fit_array : np.ndarray
        Array containing the fit
    """
    data_shape = data.shape
    if mask is None:
        mask = np.sum(data, axis=3).astype(bool)

    nbr_processes = multiprocessing.cpu_count() if nbr_processes is None \
        or nbr_processes <= 0 else nbr_processes

    # Ravel the first 3 dimensions while keeping the 4th intact, like a list of
    # 1D time series voxels. Then separate it in chunks of len(nbr_processes).
    data = data[mask].reshape((np.count_nonzero(mask), data_shape[3]))
    chunks = np.array_split(data, nbr_processes)

    chunk_len = np.cumsum([0] + [len(c) for c in chunks])
    pool = multiprocessing.Pool(nbr_processes)
    results = pool.map(fit_gamma_parallel,
                       zip(chunks,
                           itertools.repeat(gtab_infos),
                           itertools.repeat(fit_iters),
                           itertools.repeat(random_iters),
                           itertools.repeat(do_weight_bvals),
                           itertools.repeat(do_weight_pa),
                           itertools.repeat(do_multiple_s0),
                           np.arange(len(chunks))))
    pool.close()
    pool.join()

    # Re-assemble the chunk together in the original shape.
    fit_array = np.zeros((data_shape[0:3])+(4,))
    tmp_fit_array = np.zeros((np.count_nonzero(mask), 4))
    for i, fit in results:
        tmp_fit_array[chunk_len[i]:chunk_len[i+1]] = fit

    fit_array[mask] = tmp_fit_array

    return fit_array
