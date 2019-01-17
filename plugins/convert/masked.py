#!/usr/bin/env python3
""" Masked converter for faceswap.py
    Based on: https://gist.github.com/anonymous/d3815aba83a8f79779451262599b0955
    found on https://www.reddit.com/r/deepfakes/ """

import logging
import cv2
import numpy

numpy.set_printoptions(threshold=numpy.nan)
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Convert():
    """ Swap a source face with a target """
    def __init__(self, encoder, arguments, input_size=64):
        logger.debug("Initializing %s: (encoder: '%s', arguments: %s, input_size: %s)",
                     self.__class__.__name__, encoder, arguments, input_size)
        self.encoder = encoder
        self.input_size = input_size
        self.blur_size = arguments.blur_size
        self.erosion_size = arguments.erosion_size
        self.draw_transparent = arguments.draw_transparent
        self.seamless_clone = False if self.draw_transparent else arguments.seamless_clone
        self.sharpen_image = arguments.sharpen_image
        self.avg_color_adjust = arguments.avg_color_adjust
        self.match_histogram = arguments.match_histogram
        self.mask_type = arguments.mask_type.lower()
        self.coverage_ratio = arguments.coverage

        self.crop = None
        self.mask = None
        logger.debug("Initialized %s", self.__class__.__name__)

    def patch_image(self, image, face_detected):
        """ Patch the image """
        logger.trace("Patching image")
        image_size = image.shape[1], image.shape[0]
        image = image.astype('float32')
        training_size = 256  # TODO make this changeable based on extract/train settings
        training_coverage = 160 # TODO make this changeable based on extract/train settings
        coverage = int(self.coverage_ratio * training_size)
        padding = (training_size - training_coverage) // 2
        logger.trace("image_size: %s, training_size: %s, coverage: %s, padding: %s",
                     image_size, training_size, coverage, padding)

        self.crop = slice(padding, training_size - padding)
        if not self.mask:  # Init the mask on first image
            self.mask = Mask(self.mask_type, training_size, padding, self.crop)

        face_detected.load_aligned(None, training_size, padding, align_eyes=False)
        matrix = face_detected.aligned["matrix"] * (training_size - 2 * padding)
        matrix[:, 2] += padding

        interpolators = self.get_matrix_scaling(matrix)

        new_image = self.get_new_image(image, matrix, training_size,
                                       image_size, interpolators, coverage)

        image_mask = self.get_image_mask(matrix, interpolators, face_detected)

        patched_face = self.apply_fixes(image, new_image, image_mask, image_size)

        logger.trace("Patched image")
        return patched_face

    @staticmethod
    def get_matrix_scaling(mat):
        """ Get the correct interpolator """
        x_scale = numpy.sqrt(mat[0, 0] * mat[0, 0] + mat[0, 1] * mat[0, 1])
        y_scale = (mat[0, 0] * mat[1, 1] - mat[0, 1] * mat[1, 0]) / x_scale
        avg_scale = (x_scale + y_scale) * 0.5
        if avg_scale > 1.0:
            interpolator = cv2.INTER_CUBIC  # pylint: disable=no-member
            inverse_interpolator = cv2.INTER_AREA  # pylint: disable=no-member
        else:
            interpolator = cv2.INTER_AREA  # pylint: disable=no-member
            inverse_interpolator = cv2.INTER_CUBIC  # pylint: disable=no-member
        logger.trace("interpolator: %s, inverse_interpolator: %s",
                     interpolator, inverse_interpolator)
        return interpolator, inverse_interpolator

    def get_new_image(self, image, mat, training_size, image_size, interpolators, coverage):
        """ Get the new face from the predictor """
        logger.trace("mat: %s, training_size: %s, image_size: %s, interpolators: %s, coverage: %s",
                     mat, training_size, image_size, interpolators, coverage)
        src_face = cv2.warpAffine(image,  # pylint: disable=no-member
                                  mat,
                                  (training_size, training_size),
                                  flags=interpolators[0])
        coverage_face = src_face[self.crop, self.crop]
        coverage_face = cv2.resize(coverage_face,  # pylint: disable=no-member
                                   (self.input_size, self.input_size),
                                   interpolation=interpolators[0])
        coverage_face = numpy.expand_dims(coverage_face, 0)
        numpy.clip(coverage_face / 255.0, 0.0, 1.0, out=coverage_face)

        new_face = self.encoder(coverage_face)[0]

        new_face = cv2.resize(new_face,  # pylint: disable=no-member
                              (coverage, coverage),
                              interpolation=cv2.INTER_CUBIC)  # pylint: disable=no-member
        numpy.clip(new_face * 255.0, 0.0, 255.0, out=new_face)
        src_face[self.crop, self.crop] = new_face

        background = image.copy()
        new_image = cv2.warpAffine(  # pylint: disable=no-member
            src_face,
            mat,
            image_size,
            background,
            flags=cv2.WARP_INVERSE_MAP | interpolators[1],  # pylint: disable=no-member
            borderMode=cv2.BORDER_TRANSPARENT)  # pylint: disable=no-member
        return new_image

    def get_image_mask(self, mat, interpolators, landmarks):
        """ Get the image mask """
        mask = self.mask.get_mask(mat, interpolators, landmarks)
        if self.erosion_size != 0:
            kwargs = {'src': mask,
                      'kernel': self.set_erosion_kernel(mask),
                      'iterations': 1}
            if self.erosion_size > 0:
                mask = cv2.erode(**kwargs)  # pylint: disable=no-member
            else:
                mask = cv2.dilate(**kwargs)  # pylint: disable=no-member

        if self.blur_size != 0:
            blur_size = self.set_blur_size(mask)
            mask = cv2.blur(mask, (blur_size, blur_size))  # pylint: disable=no-member

        return numpy.clip(mask, 0.0, 1.0, out=mask)

    def set_erosion_kernel(self, mask):
        """ Set the erosion kernel """
        if abs(self.erosion_size) < 1.0:
            mask_radius = numpy.sqrt(numpy.sum(mask)) / 2
            percent_erode = max(1, int(abs(self.erosion_size * mask_radius)))
            erosion_kernel = cv2.getStructuringElement(  # pylint: disable=no-member
                cv2.MORPH_ELLIPSE,  # pylint: disable=no-member
                (percent_erode, percent_erode))
        else:
            e_size = (int(abs(self.erosion_size)), int(abs(self.erosion_size)))
            erosion_kernel = cv2.getStructuringElement(  # pylint: disable=no-member
                cv2.MORPH_ELLIPSE,  # pylint: disable=no-member
                e_size)
        logger.trace("erosion_kernel: %s", erosion_kernel)
        return erosion_kernel

    def set_blur_size(self, mask):
        """ Set the blur size to absolute or percentage """
        if self.blur_size < 1.0:
            mask_radius = numpy.sqrt(numpy.sum(mask)) / 2
            blur_size = max(1, int(self.blur_size * mask_radius))
        else:
            blur_size = self.blur_size
        logger.trace("blur_size: %s", int(blur_size))
        return int(blur_size)

    def apply_fixes(self, image, new_image, image_mask, image_size):
        """ Apply fixes """
        masked = new_image  # * image_mask

        if self.draw_transparent:
            alpha = numpy.full((image_size[1], image_size[0], 1),
                               255.0, dtype='float32')
            new_image = numpy.concatenate(new_image, alpha, axis=2)
            image_mask = numpy.concatenate(image_mask, alpha, axis=2)
            image = numpy.concatenate(image, alpha, axis=2)

        if self.sharpen_image is not None:
            numpy.clip(masked, 0.0, 255.0, out=masked)
            if self.sharpen_image == "box_filter":
                kernel = numpy.ones((3, 3)) * (-1)
                kernel[1, 1] = 9
                masked = cv2.filter2D(masked, -1, kernel)
            elif self.sharpen_image == "gaussian_filter":
                blur = cv2.GaussianBlur(masked, (0, 0), 3.0)
                masked = cv2.addWeighted(masked, 1.5, blur, -0.5, 0, masked)

        if self.avg_color_adjust:
            for _ in [0, 1]:
                numpy.clip(masked, 0.0, 255.0, out=masked)
                diff = image - masked
                avg_diff = numpy.sum(diff * image_mask, axis=(0, 1))
                adjustment = avg_diff / numpy.sum(image_mask, axis=(0, 1))
                masked = masked + adjustment

        if self.match_histogram:
            numpy.clip(masked, 0.0, 255.0, out=masked)
            masked = self.color_hist_match(masked, image, image_mask)

        if self.seamless_clone:
            h, w, _ = image.shape
            h = h // 2
            w = w // 2

            y_indices, x_indices, _ = numpy.nonzero(image_mask)
            y_crop = slice(numpy.min(y_indices), numpy.max(y_indices))
            x_crop = slice(numpy.min(x_indices), numpy.max(x_indices))
            y_center = int(numpy.rint((numpy.max(y_indices) + numpy.min(y_indices)) / 2) + h)
            x_center = int(numpy.rint((numpy.max(x_indices) + numpy.min(x_indices)) / 2) + w)

            insertion = numpy.uint8(masked[y_crop, x_crop, :])
            insertion_mask = numpy.uint8(image_mask[y_crop, x_crop, :])
            insertion_mask[insertion_mask != 0] = 255
            padded = numpy.pad(image,
                               ((h, h), (w, w), (0, 0)),
                               'constant').astype('uint8')
            blended = cv2.seamlessClone(insertion,
                                        padded,
                                        insertion_mask,
                                        (x_center, y_center),
                                        cv2.NORMAL_CLONE)
            blended = blended[h:-h, w:-w, :]
        else:
            foreground = masked * image_mask
            background = image * (1.0 - image_mask)
            blended = foreground + background

        numpy.clip(blended, 0.0, 255.0, out=blended)

        return numpy.rint(blended).astype('uint8')

    def color_hist_match(self, source, target, image_mask):
        for channel in [0, 1, 2]:
            source[:, :, channel] = self.hist_match(source[:, :, channel],
                                                    target[:, :, channel],
                                                    image_mask[:, :, channel])
        # source = numpy.stack([self.hist_match(source[:,:,c], target[:,:,c],image_mask[:,:,c])
        #                      for c in [0,1,2]],
        #                     axis=2)
        return source

    def hist_match(self, source, template, image_mask):

        outshape = source.shape
        source = source.ravel()
        template = template.ravel()
        s_values, bin_idx, s_counts = numpy.unique(source,
                                                   return_inverse=True,
                                                   return_counts=True)
        t_values, t_counts = numpy.unique(template, return_counts=True)
        s_quants = numpy.cumsum(s_counts, dtype='float32')
        t_quants = numpy.cumsum(t_counts, dtype='float32')
        s_quants /= s_quants[-1]  # cdf
        t_quants /= t_quants[-1]  # cdf
        interp_s_values = numpy.interp(s_quants, t_quants, t_values)
        source = interp_s_values[bin_idx].reshape(outshape)

        '''
        bins = numpy.arange(256)
        template_CDF, _ = numpy.histogram(template, bins=bins, density=True)
        flat_new_image = numpy.interp(source.ravel(), bins[:-1], template_CDF) * 255.0
        return flat_new_image.reshape(source.shape) * 255.0
        '''
        return source


class Mask():
    """ Return the requested mask """

    def __init__(self, mask_type, training_size, padding, crop):
        """ Set requested mask """
        logger.debug("Initializing %s: (mask_type: '%s', training_size: %s, padding: %s)",
                     self.__class__.__name__, mask_type, training_size, padding)

        self.training_size = training_size
        self.padding = padding
        self.mask_type = mask_type
        self.crop = crop

        logger.debug("Initialized %s", self.__class__.__name__)

    def get_mask(self, matrix, interpolators, detected_face):
        """ Return a face mask """
        image_size = (detected_face.frame_dims[1], detected_face.frame_dims[0])
        kwargs = {"matrix": matrix,
                  "interpolators": interpolators,
                  "landmarks": detected_face.landmarks_as_xy,
                  "image_size": image_size}
        logger.trace("kwargs: %s", kwargs)
        mask = getattr(self, self.mask_type)(**kwargs)
        mask = self.finalize_mask(mask)
        logger.trace("mask shape: %s", mask.shape)
        return mask

    def cnn(self, **kwargs):
        """ CNN Mask """
        # Insert FCN-VGG16 segmentation mask model here
        logger.info("cnn not incorporated, using facehull instead")
        return self.facehull(**kwargs)

    def smoothed(self, **kwargs):
        """ Smoothed Mask """
        logger.trace("Getting mask")
        interpolator = kwargs["interpolators"][1]
        ones = numpy.zeros((self.training_size, self.training_size, 3), dtype='float32')
        # area = self.padding + (self.training_size - 2 * self.padding) // 15
        # central_core = slice(area, -area)
        ones[self.crop, self.crop] = 1.0
        ones = cv2.GaussianBlur(ones, (25, 25), 10)  # pylint: disable=no-member

        mask = numpy.zeros((kwargs["image_size"][1], kwargs["image_size"][0], 3), dtype='float32')
        cv2.warpAffine(ones,  # pylint: disable=no-member
                       kwargs["matrix"],
                       kwargs["image_size"],
                       mask,
                       flags=cv2.WARP_INVERSE_MAP | interpolator,  # pylint: disable=no-member
                       borderMode=cv2.BORDER_CONSTANT,  # pylint: disable=no-member
                       borderValue=0.0)
        return mask

    def rect(self, **kwargs):
        """ Rect Mask """
        logger.trace("Getting mask")
        interpolator = kwargs["interpolators"][1]
        ones = numpy.zeros((self.training_size, self.training_size, 3), dtype='float32')
        mask = numpy.zeros((kwargs["image_size"][1], kwargs["image_size"][0], 3), dtype='float32')
        # central_core = slice(self.padding, -self.padding)
        ones[self.crop, self.crop] = 1.0
        cv2.warpAffine(ones,  # pylint: disable=no-member
                       kwargs["matrix"],
                       kwargs["image_size"],
                       mask,
                       flags=cv2.WARP_INVERSE_MAP | interpolator,  # pylint: disable=no-member
                       borderMode=cv2.BORDER_CONSTANT,  # pylint: disable=no-member
                       borderValue=0.0)
        return mask

    def facehull(self, **kwargs):
        """ Facehull Mask """
        logger.trace("Getting mask")
        mask = numpy.zeros((kwargs["image_size"][1], kwargs["image_size"][0], 3), dtype='float32')
        hull = cv2.convexHull(  # pylint: disable=no-member
            numpy.array(kwargs["landmarks"]).reshape((-1, 2)))
        cv2.fillConvexPoly(mask,  # pylint: disable=no-member
                           hull,
                           (1.0, 1.0, 1.0),
                           lineType=cv2.LINE_AA)  # pylint: disable=no-member
        return mask

    def facehull_rect(self, **kwargs):
        """ Facehull Rect Mask """
        logger.trace("Getting mask")
        mask = self.rect(**kwargs)
        hull_mask = self.facehull(**kwargs)
        mask *= hull_mask
        return mask

    def ellipse(self, **kwargs):
        """ Ellipse Mask """
        logger.trace("Getting mask")
        mask = numpy.zeros((kwargs["image_size"][1], kwargs["image_size"][0], 3), dtype='float32')
        ell = cv2.fitEllipse(  # pylint: disable=no-member
            numpy.array(kwargs["landmarks"]).reshape((-1, 2)))
        cv2.ellipse(mask,  # pylint: disable=no-member
                    box=ell,
                    color=(1.0, 1.0, 1.0),
                    thickness=-1)
        return mask

    @staticmethod
    def finalize_mask(mask):
        """ Finalize the mask """
        logger.trace("Finalizing mask")
        numpy.nan_to_num(mask, copy=False)
        numpy.clip(mask, 0.0, 1.0, out=mask)
        return mask
