# -*- coding: utf-8 -*-
import logging

import numpy as np


class SeedGenerator(object):
    """
    Class to get seeding positions.

    Generated seeds are in voxmm space. Ex: a seed sampled exactly at voxel
    i,j,k = (0,1,2), thus at voxel center (0.5, 1.5, 2.5), with resolution
    3x3x3mm will have coordinates x,y,z = (1.5, 4.5, 7.5)

    Seeds are placed randomly within the voxel. In the same example as above,
    seed sampled in voxel i,j,k = (0,1,2) will be somewhere in the range
    x = [0, 3], y = [3, 6], z = [6, 9].
    """
    def __init__(self, img):
        """
        Parameters
        ----------
        img: nibabel image
            The seeding mask. Seeds will be randomly placed in voxels with
            value >0.
        """
        self.pixdim = img.header.get_zooms()[:3]

        data = img.get_fdata(caching='unchanged', dtype=np.float64)

        # self.seed_voxels are all the voxels where a seed could be placed
        # (voxel space, int numbers). Sending "to center" by adding
        # 0.5, 0.5, 0.5.
        self.seeds = np.array(np.where(np.squeeze(data) > 0),
                              dtype=float).transpose() + 0.5
        if len(self.seeds) == 0:
            logging.warning("There are positive voxels in the seeding mask!")

    def get_next_pos(self, random_generator, indices, which_seed):
        """
        Generate the next seed position.

        Parameters
        ----------
        random_generator : numpy random generator
            Initialized numpy number generator.
        indices : List
            Indices of current seeding map.
        which_seed : int
            Seed number to be processed.

        Return
        ------
        seed_pos: tuple
            Position of next seed expressed in mm.
        """
        len_seeds = len(self.seeds)
        if len_seeds == 0:
            return []

        half_voxel_dim = np.asarray(self.pixdim) / 2

        # Voxel selection from the seeding mask
        ind = which_seed % len_seeds
        x, y, z = self.seeds[indices[ind]]

        # Subvoxel initial positioning
        r_x = random_generator.uniform(-half_voxel_dim[0], half_voxel_dim[0])
        r_y = random_generator.uniform(-half_voxel_dim[1], half_voxel_dim[1])
        r_z = random_generator.uniform(-half_voxel_dim[2], half_voxel_dim[2])

        return x * self.pixdim[0] + r_x, y * self.pixdim[1] \
            + r_y, z * self.pixdim[2] + r_z

    def init_generator(self, random_initial_value, first_seed_of_chunk):
        """
        Initialize numpy number generator according to user's parameter
        and indexes from the seeding map.

        Parameters
        ----------
        random_initial_value : int
            The "seed" for the random generator.
        first_seed_of_chunk : int
            Number of seeds to skip (skip parameter + multi-processor skip).

        Return
        ------
        random_generator : numpy random generator
            Initialized numpy number generator.
        indices : List
            Indices of current seeding map.
        """
        random_generator = np.random.RandomState(random_initial_value)

        # 1. Initializing seeding maps indices (shuffling in-place)
        indices = np.arange(len(self.seeds))
        random_generator.shuffle(indices)

        # 2. Initializing the random generator
        # For reproducibility through multi-processing, skipping random numbers
        # (by producing rand numbers without using them) until reaching this
        # process (i.e this chunk)'s set of random numbers. Producing only
        # 100000 at the time to prevent RAM overuse.
        # (Multiplying by 3 for x,y,z)
        random_numbers_to_skip = first_seed_of_chunk * 3
        # toDo: see if 100000 is ok, and if we can create something not
        #  hard-coded
        while random_numbers_to_skip > 100000:
            random_generator.random_sample(100000)
            random_numbers_to_skip -= 100000
        random_generator.random_sample(random_numbers_to_skip)

        return random_generator, indices
