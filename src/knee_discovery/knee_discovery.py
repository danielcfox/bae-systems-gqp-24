#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Oct  1 12:47:32 2024

@author: dfox
"""

import math
import numpy as np
import os
import pandas as pd
from pathlib import Path
import shutil

from kneed import KneeLocator
from PIL import Image

# from util.util import get_preprocessed_images_dir_path, calc_degradation_factor
from eval.eval import run_eval, update_knee_results

exts = ('.tif', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff')  # Common image file extensions
label_ext = '.txt'

def replace_image_ext_with_label(filepath):
    """
    Replaces the file extension of an image file with a label extension if it matches specified extensions.

    Args:
        filepath (str or Path): The file path of the image whose extension needs to be replaced.

    Returns:
        Path: A Path object with the file extension replaced by `label_ext` if it matches specified extensions.
              Otherwise, returns the original filepath.
    """
    path = Path(filepath)

    if path.suffix.lower() in exts:
        return path.with_suffix(label_ext)

    return filepath

def degrade_images(ctxt, orig_image_size, degraded_image_size, degraded_dir, corrupted_counter):
    """
    Degrades images by resizing them and stores them in the specified degraded directory.
    If `orig_image_size` and `degraded_image_size` are the same, images are simply copied.
    Otherwise, images are resized to the degraded size and then resized back to the original size.

    Args:
        ctxt: The pipeline context object containing configurations and settings.
        orig_image_size (tuple): A tuple (width, height) of the original image size.
        degraded_image_size (tuple): A tuple (width, height) of the degraded image size.
        degraded_dir (str): Directory where the degraded images will be stored.
        corrupted_counter (int): Counter tracking corrupted or unreadable images.

    Returns:
        tuple: A tuple containing:
            - num_images (int): The number of images processed.
            - corrupted_counter (int): Updated counter for corrupted images encountered.
    """
    # orig_image_size and degraded_image_size are tuples of (width, height)

    if ctxt.verbose:
        print(f"degrade images {orig_image_size} -> {degraded_image_size} in degraded directory {degraded_dir}", flush=True)
    config = ctxt.config

    method = config['preprocess_method']
    preprocess_top_dir = ctxt.get_preprocessing_dir_path()
    val_template = config['preprocess_methods'][method]['val_baseline_subdir']
    
    # maxwidth = config['preprocess_methods'][method]['image_size']
    # maxheight = maxwidth
    maxwidth = orig_image_size[0]
    maxheight = orig_image_size[1]

    if method == 'padding':
        # ctxt.train_baseline_dir = os.path.join(preprocess_top_dir, train_template.format(maxwidth=maxwidth, maxheight=maxheight))
        val_baseline_dir = os.path.join(preprocess_top_dir, val_template.format(maxwidth=maxwidth, maxheight=maxheight))
    elif method == 'tiling':
        stride = config['preprocess_methods'][method]['stride']
        val_baseline_dir = os.path.join(preprocess_top_dir, 
                                        val_template.format(maxwidth=maxwidth, maxheight=maxheight, stride=stride))
    else:
        raise ValueError("Unknown preprocessing method: " + method)

    # baseline_dir = ctxt.val_baseline_dir
    os.makedirs(degraded_dir, exist_ok=True)
    
    if ctxt.val_image_filename_set is None:
        val_image_filenames = [os.path.join(val_baseline_dir, x) for x in os.listdir(val_baseline_dir) if x.endswith(exts)]
        ctxt.val_image_filename_set = set(val_image_filenames)
    else:
        val_image_filenames = list(ctxt.val_image_filename_set)
    num_images = len(val_image_filenames)
    val_label_filenames = [replace_image_ext_with_label(fn) for fn in val_image_filenames]
    for idx, val_image_filename in enumerate(val_image_filenames):
        val_label_filename = val_label_filenames[idx]
        if os.path.exists(val_image_filename) and os.path.exists(val_label_filename):
            val_degraded_image_filename = os.path.join(degraded_dir, os.path.basename(val_image_filename))
            val_degraded_label_filename = os.path.join(degraded_dir, os.path.basename(val_label_filename))
            if not os.path.exists(val_degraded_image_filename):
                if orig_image_size == degraded_image_size:
                    # resolutions and hyperparameters are equal, simple copy
                    shutil.copyfile(val_image_filename, val_degraded_image_filename)
                else:
                    try:
                        image = Image.open(val_image_filename)
                        val_shrunk_image = image.resize(degraded_image_size)
                        val_degraded_image = val_shrunk_image.resize(orig_image_size)
                        val_degraded_image.save(val_degraded_image_filename) 
                        image.close()
                        val_shrunk_image.close()
                        val_degraded_image.close()
                    except OSError:
                        ctxt.val_image_filename_set.discard(val_image_filename)
                        corrupted_counter += 1
                        continue
                if not os.path.exists(val_degraded_label_filename):
                    shutil.copyfile(val_label_filename, val_degraded_label_filename)
        else:
            ctxt.val_image_filename_set.discard(val_image_filename)
        
    return num_images, corrupted_counter

def run_eval_on_initial_resolutions(ctxt):
    """
    Evaluates the model on the baseline resolution as specified in the pipeline configuration
    and logs the evaluation results. This function retrieves the baseline image dimensions from 
    the configured preprocessing method and then runs the evaluation.

    Args:
        ctxt: The pipeline context object containing the pipeline configuration and relevant paths.

    Behavior:
        - Retrieves baseline resolution dimensions from the preprocessing configuration.
        - Calls `run_eval` with the baseline resolution to perform model evaluation.
        - Note: The `run_eval` function internally logs the results, so additional logging or
                calls to `update_results()` are not needed here.
    """
    config = ctxt.get_pipeline_config()
    preprocess_method = config['preprocess_method']
    pp_params = config['preprocess_methods'][preprocess_method]
    width = pp_params['image_size']
    height = pp_params['image_size']
   
    # Run evaluation on the baseline resolution
    baseline_dir = ctxt.val_baseline_dir
    run_eval(ctxt, (width, height), (width, height), baseline_dir, "unknown")
    # Note: No need to call update_results() here since run_eval() already calls it.

def run_eval_on_degraded_images(ctxt):
    """
    Evaluates the model on a range of degraded image resolutions, specified in the configuration,
    and logs the results. This function systematically reduces image resolution within the range 
    defined by the knee discovery parameters, creating degraded copies and evaluating the model on each.

    Args:
        ctxt: The pipeline context object containing configuration settings, paths, and options.

    Behavior:
        - Retrieves baseline and knee discovery parameters from the pipeline configuration.
        - Iteratively reduces resolution of images in specified steps and evaluates the model on each degraded set.
        - Logs any corrupted images that could not be processed during degradation.
    
    Log Output:
        - Prints status messages if verbosity is enabled, detailing degradation ranges, steps, and corrupted images.
        - Final report of corrupted images if any were encountered during processing.

    """

    config = ctxt.get_pipeline_config()
    preprocess_method = config['preprocess_method']
    pp_params = config['preprocess_methods'][preprocess_method]
    kd_params = config['knee_discovery']
    val_template = pp_params['val_degraded_subdir']

    width = pp_params['image_size']
    height = pp_params['image_size']
    if preprocess_method == 'tiling':
        stride = pp_params['stride']
    else:
        stride = None
    
    
    longer = max(width, height)
    shorter = min(width, height)
    shorter_mult = shorter / longer

    long_low_range = math.ceil(kd_params['search_resolution_range'][0] * longer)
    long_high_range = math.ceil(kd_params['search_resolution_range'][1] * longer) + 1
    step = math.floor(kd_params['search_resolution_step'] * longer)

    # results = []

    if ctxt.verbose:
        print(f"degrading images from {long_low_range} to {long_high_range} step {step}")
    corrupted_counter = 0
    max_images = 0
    for degraded_long_res in range(long_low_range, long_high_range, step):
        degraded_short_res = math.ceil(shorter_mult * degraded_long_res)
        degraded_width = degraded_long_res if width == longer else degraded_short_res
        degraded_height = degraded_short_res if height == longer else degraded_long_res

        # Create directory path for degraded images
        val_degraded_dir = os.path.join(ctxt.get_preprocessing_dir_path(), val_template.format(
            maxwidth=width, maxheight=height, effective_width=degraded_width, effective_height=degraded_height, stride=stride))

        # Degrade images and run evaluation
        num_images, corrupted_counter = degrade_images(ctxt, (width, height), (degraded_width, degraded_height), val_degraded_dir, 
                                                       corrupted_counter)
        if max_images == 0:
            max_images = num_images
        run_eval(ctxt, (width, height), (degraded_width, degraded_height), val_degraded_dir, "unknown")

    if corrupted_counter > 0:
        print(f"{corrupted_counter} out of {max_images} images are corrupted!")
    

def calc_degradation_factor(orig_res_w, orig_res_h, eff_res_w, eff_res_h):
    """
    Calculates the degradation factor based on the original and effective image resolutions.
    The degradation factor is computed as the square root of the area ratio between the effective 
    and original resolutions.

    Args:
        orig_res_w (float or pd.Series): Original resolution width.
        orig_res_h (float or pd.Series): Original resolution height.
        eff_res_w (float or pd.Series): Effective resolution width.
        eff_res_h (float or pd.Series): Effective resolution height.
    
    Returns:
        pd.Series: The degradation factor calculated from the resolutions, representing the ratio
                   of effective resolution to original resolution.
    """
    
    degradation_factor_w = eff_res_w / orig_res_w
    degradation_factor_h = eff_res_h / orig_res_h
    degradation_factor_area = degradation_factor_w * degradation_factor_h
    degradation_factor = degradation_factor_area.apply(math.sqrt)
    return degradation_factor # pd.Series

def calculate_knee(ctxt, class_name, results_class_df):
    """
    Calculates the "knee" point in the IAPC curve, where mAP (mean Average Precision) values
    exhibit a significant change, indicating an optimal balance between resolution and performance.
    
    Args:
        ctxt: The pipeline context object containing configuration and verbosity settings.
        class_name (str): Name of the class for which the knee is being calculated.
        results_class_df (pd.DataFrame): DataFrame with columns 'original_resolution_width', 
                                         'original_resolution_height', 'effective_resolution_width',
                                         'effective_resolution_height', and 'mAP' for the given class.

    Returns:
        tuple: A tuple containing two lists:
            - x_out_list (list): A list of degradation factors where knee points are identified.
            - y_out_list (list): A list of corresponding mAP values at the identified knee points.

    Behavior:
        - Converts resolution columns to floating-point values and calculates degradation factors.
        - Filters out mAP values below a threshold (0.01) to focus on meaningful data points.
        - Identifies knee points using the `KneeLocator` to detect the "elbow" in the degradation vs. mAP curve.
        - Logs knee points if verbosity is enabled in the context.
        - Calls `update_knee_results` for each knee to store results in the context.

    """
    orig_res_w = results_class_df['original_resolution_width'].astype(float)
    orig_res_h = results_class_df['original_resolution_height'].astype(float)
    eff_res_w = results_class_df['effective_resolution_width'].astype(float)
    eff_res_h = results_class_df['effective_resolution_height'].astype(float)
    mAP_values_series = results_class_df['mAP']
    degradation_factor_series = calc_degradation_factor(orig_res_w, orig_res_h, eff_res_w, eff_res_h)
    degradation_factor_list = degradation_factor_series.to_list()
    
    mAP_values = mAP_values_series.to_list()
    width_list = orig_res_w.to_list()
    height_list = orig_res_h.to_list()

    # if all mAP are zero, return any value from degradation factor list, let's pick the minimum
    if all(mAP <= 0.01 for mAP in mAP_values):
        return [], []
    
    x = [d for i, d in enumerate(degradation_factor_list) if mAP_values[i] > 0.01]
    y = [m for m in mAP_values if m > 0.01]
    w = [w for i, w in enumerate(width_list) if mAP_values[i] > 0.01]
    h = [h for i, h in enumerate(height_list) if mAP_values[i] > 0.01]
    
    if len(x) == 0 or len(y) == 0:
        return [], []

    # kneedle = KneeLocator(x_interp, y_interp, curve='concave', direction='increasing')

    # kneedle = KneeLocator(degradation_factor_list, mAP_values, curve='concave', direction='increasing')
    kneedle = KneeLocator(x, y, curve='concave', direction='increasing', online=True)
    x_out_list = list(sorted(kneedle.all_knees))
    y_out_list = kneedle.all_knees_y
    if ctxt.verbose:
        print("Knees:")
        print(f"  {x_out_list}")
        print(f"  {y_out_list}")
    
    for i, x in enumerate(x_out_list):
        update_knee_results(ctxt, class_name, (w[0], h[0]), x, y_out_list[i])
        
    return x_out_list, y_out_list
    
def run_knee_discovery(ctxt):
    """
    Runs the knee discovery process to identify optimal image resolutions at which model performance 
    (mean Average Precision, mAP) experiences a significant change. This process iteratively degrades 
    image resolution, evaluates the model, and identifies knee points in the performance curve.
    
    Args:
        ctxt: The pipeline context object containing configurations, paths, and verbosity settings.

    Behavior:
        - Initializes output paths and clears previous knee discovery results if configured to do so.
        - Runs evaluation on initial degraded resolutions to populate a baseline.
        - Loads or calculates degradation factors for each resolution.
        - Iterates over each class to identify the knee point in the degradation curve.
            - If a binary search algorithm is specified, it refines the knee point by iterating over nearby 
              degradation factors until convergence is achieved within a specified tolerance or a maximum 
              number of iterations.
            - Uses `KneeLocator` to identify knee points and logs details if verbosity is enabled.
        - Updates and saves the final knee discovery results to the specified output file.

    Log Output:
        - Prints status updates for the knee discovery process, including convergence checks and detected knee points.
        - Prints a summary of discovered knee points or a notification if no knee point is found for a class.

    Returns:
        None
    """
    print("Start with run_knee_discovery")
    
    config = ctxt.get_pipeline_config()
    output_top_dir = ctxt.get_output_dir_path()
    results_path = os.path.join(output_top_dir, config['knee_discovery']['output_subdir'])
    eval_results_filename = os.path.join(results_path, config['knee_discovery']['eval_results_filename'])
    
    if 'clean_subdir' in config['knee_discovery'] and config['knee_discovery']['clean_subdir']:
        if os.path.exists(results_path):
            shutil.rmtree(results_path)

    os.makedirs(results_path, exist_ok=True)
    
    # Run evaluation on initial (baseline) resolutions
    run_eval_on_degraded_images(ctxt)

    # Load the results from CSV (or from the cached results in memory)
    if ctxt.results_cache_df is not None:
        results_df = ctxt.results_cache_df
    else:
        if os.path.exists(eval_results_filename):
            results_df = pd.read_csv(eval_results_filename, index_col=False)
        else: 
            print(f"Knee discovery: {eval_results_filename} not found!")
            return
        # results = results_df.to_dict('records')

    ## Ensure degradation_factor is calculated
    if 'degradation_factor' not in results_df.columns:
        orig_res_w = results_df['original_resolution_width'].astype(float)
        orig_res_h = results_df['original_resolution_height'].astype(float)
        eff_res_w = results_df['effective_resolution_width'].astype(float)
        eff_res_h = results_df['effective_resolution_height'].astype(float)
        results_df['degradation_factor'] = calc_degradation_factor(orig_res_w, orig_res_h, eff_res_w, eff_res_h)

    # Ensure 'knee' column exists
    if 'knee' not in results_df.columns:
        results_df['knee'] = False

    class_names = results_df['object_name'].unique()

    # Set the desired convergence tolerance and maximum iterations
    desired_tolerance = 1e-2  # Adjust this value based on your needs
    mAP_tolerance = 1e-3      # Tolerance for mAP change convergence
    max_iterations = 10       # Maximum number of iterations to prevent excessive computations

    results_df['knee'] = False
    for class_name in class_names:
        results_class_df = results_df[results_df['object_name'] == class_name].copy()
        x_list, y_list = calculate_knee(ctxt, class_name, results_class_df)
        if len(x_list) > 0 and len(y_list) > 0:
            knee_degradation_factor = x_list[0]
        else:
            knee_degradation_factor = None
        
        # Initialize variables for convergence checking
        if ctxt.knee_discovery_search_algorithm == 'binary':
            new_knee_degradation_factor = knee_degradation_factor
            old_knee_degradation_factor = None
            new_knee_mAP = None
            old_knee_mAP = None
            iteration = 0

            while (iteration < max_iterations and new_knee_degradation_factor is not None and
                    (old_knee_degradation_factor is None or not np.isclose(new_knee_degradation_factor, 
                                                                           old_knee_degradation_factor, 
                                                                           atol=desired_tolerance))):
                iteration += 1
                old_knee_degradation_factor = new_knee_degradation_factor

                # Implement the knee discovery algorithm here
                # Find neighboring degradation factors
                degradation_factors = results_class_df['degradation_factor'].values
                mAP_values = results_class_df['mAP'].values

                sorted_indices = np.argsort(degradation_factors)
                degradation_factors_sorted = degradation_factors[sorted_indices]
                mAP_values_sorted = mAP_values[sorted_indices]

                # Find index of current knee degradation factor
                knee_index = np.where(np.isclose(degradation_factors_sorted, new_knee_degradation_factor, atol=1e-5))[0]

                if knee_index.size == 0:
                    if ctxt.verbose:
                        print(f"Knee degradation factor {new_knee_degradation_factor} not found in degradation factors.")
                    break

                knee_index = knee_index[0]

                # Get mAP at the knee degradation factor
                new_knee_mAP = mAP_values_sorted[knee_index]

                # If old_knee_mAP is set, check mAP change for convergence
                if old_knee_mAP is not None:
                    mAP_change = abs(new_knee_mAP - old_knee_mAP)
                    if mAP_change < mAP_tolerance:
                        if ctxt.verbose:
                            print(f"Convergence achieved based on mAP change for class {class_name}.")
                        break

                old_knee_mAP = new_knee_mAP

                # Find left and right indices
                if knee_index > 0:
                    left_index = knee_index - 1
                    left_degradation = degradation_factors_sorted[left_index]
                    left_mAP = mAP_values_sorted[left_index]
                    delta_mAP_left = abs(new_knee_mAP - left_mAP)
                else:
                    left_degradation = None
                    delta_mAP_left = 0

                if knee_index < len(degradation_factors_sorted) - 1:
                    right_index = knee_index + 1
                    right_degradation = degradation_factors_sorted[right_index]
                    right_mAP = mAP_values_sorted[right_index]
                    delta_mAP_right = abs(new_knee_mAP - right_mAP)
                else:
                    right_degradation = None
                    delta_mAP_right = 0

                # Decide which side has higher mAP difference
                if delta_mAP_left > delta_mAP_right and left_degradation is not None:
                    # Choose left side
                    degradation1 = left_degradation
                    degradation2 = new_knee_degradation_factor
                elif right_degradation is not None:
                    # Choose right side
                    degradation1 = new_knee_degradation_factor
                    degradation2 = right_degradation
                else:
                    # Cannot refine further
                    if ctxt.verbose:
                        print("No further refinement possible.")
                    break

                # Calculate new degradation factor between selected points
                new_degradation = (degradation1 + degradation2) / 2

                # Check if this degradation factor is already in our data
                if np.any(np.isclose(degradation_factors, new_degradation, atol=1e-4)):
                    # This degradation factor is already in the data, cannot proceed
                    if ctxt.verbose:
                        print(f"Degradation factor {new_degradation} already evaluated.")
                    break

                # Convert degradation factor to degraded width and height
                width = results_class_df['original_resolution_width'].iloc[0]
                height = results_class_df['original_resolution_height'].iloc[0]
                degraded_width = int(new_degradation * width)
                degraded_height = int(new_degradation * height)

                # Ensure degraded dimensions are within valid ranges
                degraded_width = max(1, min(int(width), degraded_width))
                degraded_height = max(1, min(int(height), degraded_height))

                # Degrade images at this new resolution and run evaluation
                preprocess_method = config['preprocess_method']
                pp_params = config['preprocess_methods'][preprocess_method]
                val_template = pp_params['val_degraded_subdir']
                stride = pp_params.get('stride', None)
                val_degraded_dir = os.path.join(ctxt.get_preprocessing_dir_path(), val_template.format(
                    maxwidth=width, maxheight=height, effective_width=degraded_width, effective_height=degraded_height, stride=stride))

                num_images, corrupted_counter = degrade_images(ctxt, (width, height), (degraded_width, degraded_height), val_degraded_dir, 0)
                run_eval(ctxt, (width, height), (degraded_width, degraded_height), val_degraded_dir, "unknown")
                # Note: No need to call update_results() here since run_eval() already calls it.

                # After updating results, refresh results_df
                if ctxt.results_cache_df is not None:
                    results_df = ctxt.results_cache_df
                else:
                    if os.path.exists(eval_results_filename):
                        results_df = pd.read_csv(eval_results_filename, index_col=False)
                    else:
                        print(f"Knee discovery: {eval_results_filename} not found!")
                        break

                results_class_df = results_df[results_df['object_name'] == class_name]

                # Recalculate the knee
                x_list, y_list = calculate_knee(ctxt, class_name, results_class_df)
                if len(x_list) > 0 and len(y_list) > 0:
                    new_knee_degradation_factor = x_list[0]
                else:
                    new_knee_degradation_factor = None
                    

            # Mark the knee point
            results_df.loc[results_df['object_name'] == class_name, 'knee'] = False
            if new_knee_degradation_factor is not None:
                df = results_class_df[np.isclose(results_class_df['degradation_factor'], new_knee_degradation_factor, atol=1e-5)]
                results_df.loc[df.index, 'knee'] = True

                if ctxt.verbose:
                    print(f"Knee discovered at degradation factor {new_knee_degradation_factor} for {class_name}")

                # Plot the IAPC curve for the class
                # plot_iapc_curve(results_class_df, class_name)
            elif ctxt.verbose:
                print(f"No knee found for class {class_name}")

        else: # knee algorithm not specified, so none

            # Mark the knee point
            if knee_degradation_factor is not None:
                df = results_class_df[np.isclose(results_class_df['degradation_factor'], knee_degradation_factor, atol=1e-5)]
                results_df.loc[df.index, 'knee'] = True

                if ctxt.verbose:
                    print(f"Knee discovered at degradation factor {knee_degradation_factor} for {class_name}")

                # Plot the IAPC curve for the class
                # plot_iapc_curve(results_class_df, class_name)
            elif ctxt.verbose:
                print(f"No knee found for class {class_name}")

    results_df.to_csv(eval_results_filename, index=False)
    if ctxt.cache_results:
        ctxt.results_cache_df = results_df.copy()

    print("End knee discovery")
