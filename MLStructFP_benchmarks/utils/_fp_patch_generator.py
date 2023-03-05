"""
MLSTRUCTFP BENCHMARKS - UTILS - PATCH GENERATOR

Generate image patches (binary, images) for a given set of floor plans.

The patches are computed by setting a patch size, fixed for x/y axes, thus,
only square-images are valid. Then it proposes a set of patches, where for
each one several images are obtained using a displacement which cannot exceed
50%. To save the results, the object stores the images in an efficient data
storage object npz, which can be re-loaded to train the models.
"""

__all__ = ['FloorPatchGenerator']

from MLStructFP.db.image import RectBinaryImage, RectFloorPhoto
# noinspection PyProtectedMember
from MLStructFP.db.image._rect_photo import RectFloorShapeException
from MLStructFP.utils import DEFAULT_PLOT_DPI, configure_figure

import gc
import matplotlib.pyplot as plt
import math
import numpy as np

from typing import List, Tuple, Optional, TYPE_CHECKING, Union
from warnings import warn

if TYPE_CHECKING:
    from MLStructFP.db import Floor

PatchRectType = List[Tuple[int, bool, float, float, float, float, int]]


class FloorPatchGenerator(object):
    """
    Patch generator.
    """
    _bw: bool  # Convert to black/white
    _dx: List[float]
    _dy: List[float]
    _gen_binary: 'RectBinaryImage'
    _gen_photo: 'RectFloorPhoto'
    _image_size: int
    _img_size: int
    _min_binary_area: float
    _patch_binary: List['np.ndarray']
    _patch_photo: List['np.ndarray']
    _patch_size: float
    _test_ignored_patches: List[int]
    _test_last_added: int

    def __init__(
            self,
            image_size: int,
            patch_size: float,
            bw: bool,
            delta_x: Optional[Union[List[float], Tuple[float, ...]]] = None,
            delta_y: Optional[Union[List[float], Tuple[float, ...]]] = None,
            min_binary_area: float = 0
    ) -> None:
        """
        Constructor.

        :param image_size: Image size in px. Must be a power of 2
        :param patch_size: Dimension in (m) to crop the floor plan for x/y-axis
        :param bw: Convert all images to black/white. Recomended as color does not contribute to the plan semantics
        :param delta_x: Delta crops/sliding-window for each patch (from -0.5,-0.5). If None, only iterate in y-axis
        :param delta_y: Delta crops for each patch (from -0.5,-0.5). If both are None, there is only 1 crop per patch
        :param min_binary_area: Min area for binary image allowed (0-1). If lower, ignores the patch
        """
        assert isinstance(bw, bool)
        assert patch_size > 0, 'patch size cannot be negative'
        if delta_x is None:
            delta_x = [0]
        if delta_y is None:
            delta_y = [0]
        if isinstance(delta_x, tuple):
            delta_x = list(delta_x)
        if isinstance(delta_y, tuple):
            delta_y = list(delta_y)
        assert isinstance(delta_x, list), 'delta x must be an increasing list of float values between -0.5 to 0.5'
        assert isinstance(delta_y, list), 'delta y must be an increasing list of float values between -0.5 to 0.5'
        lx, ly = len(delta_x), len(delta_y)
        assert lx * ly != 0, 'deltas for x and y axis must have at least 1 item'
        if 0 not in delta_x:
            warn('Delta x must contain 0, which was added automatically')
            delta_x.append(0)
        if 0 not in delta_y:
            warn('Delta y must contain 0, which was added automatically')
            delta_y.append(0)
        for i in range(lx):
            assert isinstance(delta_x[i], (int, float))
            delta_x[i] = float(delta_x[i])
            assert 0 <= abs(delta_x[i]) <= 0.5, 'each delta on x-axis must be +-0.5 max'
            if i < lx - 1:
                assert delta_x[i] < delta_x[i + 1], 'delta x vector must be increasing'
        for j in range(ly):
            assert isinstance(delta_y[j], (int, float))
            delta_y[j] = float(delta_y[j])
            assert 0 <= abs(delta_y[j]) <= 0.5, 'each delta on y-axis must be +-0.5 max'
            if j < ly - 1:
                assert delta_y[j] < delta_y[j + 1], 'delta y vector must be increasing'
        assert 0 <= min_binary_area < 1, 'min area must be between 0 and 1. A zero-value accept all patches'
        self._bw = bw
        self._dx = delta_x
        self._dy = delta_y
        self._image_size = image_size
        self._gen_binary = RectBinaryImage(image_size_px=image_size)
        self._gen_photo = RectFloorPhoto(image_size_px=image_size, empty_color=0)
        self._min_binary_area = min_binary_area
        self._patch_binary = []
        self._patch_photo = []
        self._patch_size = patch_size
        self._test_ignored_patches = []
        self._test_last_added = 0

    def _process_photo(self, xmin: float, xmax: float, ymin: float, ymax: float, floor: 'Floor') -> 'np.ndarray':
        """
        Generate image for a given region.

        :param xmin: Minimum x-axis (image coordinates)
        :param xmax: Maximum x-axis (image coordinates)
        :param ymin: Minimum y-axis (image coordinates)
        :param ymax: Maximum y-axis (image coordinates)
        :param floor: Floor object
        :return: Returns the image. Can be color or in black/white
        """
        photo = self._gen_photo.make_region(xmin, xmax, ymin, ymax, floor)[1]
        if self._bw:
            photo = np.dot(photo[..., :3], [1 / 3, 1 / 3, 1 / 3])
            photo = np.where(photo > 0, 1, 0)
        return photo

    def _make_patches(self, floor: 'Floor', apply_delta: bool = True) -> PatchRectType:
        """
        Make patches.

        :param floor: Floor object
        :param apply_delta: Apply dx, dy
        :return: List of patches [(number, xmin, xmax, ymin, ymax), ...]
        """
        patches = []
        bb = floor.bounding_box
        nx = math.ceil((bb.xmax - bb.xmin) / self._patch_size)
        ny = math.ceil((bb.ymax - bb.ymin) / self._patch_size)
        n = 1  # Number of patches
        m = 1  # Patch ID
        for i in range(nx):
            for j in range(ny):
                for dx in self._dx:
                    for dy in self._dy:
                        origin = dx == 0 and dy == 0
                        if not apply_delta and not origin:
                            continue
                        patches.append((
                            n,  # Patch number
                            origin,  # Patch is origin
                            bb.xmin + self._patch_size * (i + dx),  # x-min
                            bb.xmin + self._patch_size * (i + 1 + dx),  # x-max
                            bb.ymin + self._patch_size * (j + dy),  # y-min
                            bb.ymin + self._patch_size * (j + 1 + dy),  # y-max
                            m  # Patch ID
                        ))
                        n += 1
                m += 1
        return patches

    def clear(self) -> 'FloorPatchGenerator':
        """
        Clear stored data.

        :return: Self
        """
        self._patch_binary.clear()
        self._patch_photo.clear()
        self._test_ignored_patches.clear()
        self._test_last_added = 0
        gc.collect()
        return self

    def process(self, floor: 'Floor') -> 'FloorPatchGenerator':
        """
        Process a given floor.

        :param floor: Floor to process
        :return: Self
        """
        self._test_ignored_patches.clear()
        self._gen_binary.init()
        added = 0
        for p in self._make_patches(floor, apply_delta=True):
            ignore = False
            n, origin, xmin, xmax, ymin, ymax, _ = p
            patch_b = self._gen_binary.make_region(xmin, xmax, ymin, ymax, floor)[1]
            try:
                patch_p = self._process_photo(xmin, xmax, ymin, ymax, floor)
            except RectFloorShapeException:  # Try to crop a larger plan
                ignore = True
                patch_p = patch_b
            sb, sp = np.sum(patch_b), np.sum(patch_p)

            # Avoid save empty data
            if sb == 0 and sp == 0:
                ignore = True

            # Normalize pixel sum
            total_area = (np.shape(patch_b)[0] ** 2)
            sb /= total_area
            if self._bw:
                sp /= total_area

            # Ignore if area requirement not fulfilled
            if self._min_binary_area > 0 and sb / sb <= self._min_binary_area:
                ignore = True
            if self._bw and sb < 0.001:
                ignore = True

            if ignore:
                self._test_ignored_patches.append(n)
                continue

            self._patch_binary.append(patch_b)
            self._patch_photo.append(patch_p)
            added += 1

        self._test_last_added = added
        self._gen_binary.close()
        self._gen_photo.close()
        self._gen_binary.restore_plot()

        return self

    def plot_patch(self, idx: int, inverse: bool = False) -> None:
        """
        Plot a given pair of binary/photo images.

        :param idx: Index of the image pair
        :param inverse: If true, plot inversed colors (white as background)
        """
        plt.figure(dpi=DEFAULT_PLOT_DPI)
        photo = self._patch_photo[idx]
        binary = self._patch_binary[idx]
        if inverse:
            if not self._bw:
                photo = 255 - photo
            else:
                photo = 1 - photo
            binary = 1 - binary
        plt.subplot(121), plt.imshow(photo, cmap='gray' if self._bw else None)
        plt.subplot(122), plt.imshow(binary, cmap='gray')

    def plot_photo(self, idx: int, inverse: bool = False) -> None:
        """
        Plot a single photo from a given patch.

        :param idx: Index of the image pair
        :param inverse: If true, plot inversed colors (white as background)
        """
        plt.figure(dpi=DEFAULT_PLOT_DPI)
        photo = self._patch_photo[idx]
        if inverse:
            if not self._bw:
                photo = 255 - photo
            else:
                photo = 1 - photo
        plt.axis('off')
        plt.imshow(photo, cmap='gray' if self._bw else None)

    def plot_patches(
            self,
            floor: 'Floor',
            photo: float = 0,
            patches: bool = True,
            patches_id: int = -1,
            rect: bool = True,
            inverse: bool = True
    ) -> None:
        """
        Plot the patches of a given floor.

        :param floor: Floor to plot
        :param photo: Opacity of the photo crops, if 0: do not display. Max: 1
        :param patches: Add patches
        :param patches_id: If defined (>0), plot only the given patch ID
        :param rect: Plot rects
        :param inverse: If true, plot inversed colors (white as background)
        """
        assert 0 <= photo <= 1, 'Photo opacity must be between 0 and 1'
        ax: 'plt.Axes'
        fig, ax = plt.subplots(dpi=DEFAULT_PLOT_DPI)
        ax.set_aspect('equal')
        if not inverse:
            ax.set_facecolor('#000000')
        for r in floor.rect:
            r.plot_matplotlib(ax, color='#000000' if not photo else '#ffffff')
        if patches:
            for p in self._make_patches(floor, apply_delta=True):
                n, origin, xmin, xmax, ymin, ymax, pid = p
                if 0 < patches_id != pid:
                    continue
                ax.plot([xmin, xmax, xmax, xmin, xmin], [ymin, ymin, ymax, ymax, ymin], '-' if origin else '--',
                        linewidth=1.5 if origin else 0.4, color='#ff0000' if origin else '#0000ff')
        if photo > 0:
            lim_x = ax.get_xlim()
            lim_y = ax.get_ylim()
            for p in self._make_patches(floor, apply_delta=False):  # Add images
                _, _, xmin, xmax, ymin, ymax, _ = p
                pimg = self._process_photo(xmin, xmax, ymin, ymax, floor)
                if inverse:
                    if not self._bw:
                        pimg = 255 - pimg
                    else:
                        pimg = 1 - pimg
                plt.imshow(pimg, cmap='gray' if self._bw else None,
                           extent=[xmin, xmax, ymin, ymax], origin='upper', alpha=photo)
            plt.xlim(lim_x)
            plt.ylim(lim_y)
        if rect:
            for r in floor.rect:
                r.plot_matplotlib(ax, color='#000000' if (not (photo and not inverse)) else '#ff00ff')
        plt.xlabel('x (m)')
        plt.ylabel('y (m)')
        configure_figure(cfg_grid=False)
