
# Copyright 2022 Image Analysis Lab, German Center for Neurodegenerative Diseases (DZNE), Bonn
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# IMPORTS
import time
from os import makedirs, cpu_count
from os.path import join, dirname, isfile
from typing import Dict, List, Tuple, Optional, Literal
from concurrent.futures import Future, ThreadPoolExecutor

import nibabel as nib
import numpy as np
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm

from FastSurferCNN.utils import logging
from FastSurferCNN.utils.mapper import JsonColorLookupTable, TSVLookupTable
from FastSurferCNN.utils.common import find_device, SubjectList, SubjectDirectory, NoParallelExecutor
from CerebNet.data_loader.augmentation import ToTensorTest
from CerebNet.data_loader.dataset import SubjectDataset, Plane, LocalizerROI, PLANES
from CerebNet.datasets.utils import crop_transform, load_reorient_lia
from CerebNet.models.networks import build_model
from CerebNet.utils import checkpoint as cp

logger = logging.get_logger(__name__)


class Inference:
    def __init__(self, cfg: 'yacs.ConfigNode',
                 threads: int = -1, async_io: bool = False, device: str = 'auto', viewagg_device: str = 'auto'):
        """
        Create the inference object to manage inferencing, batch processing, data loading, etc.

        Args:
            cfg: yaml configuration to populate default values for parameters
            threads: number of threads to use, -1 is max (all), which is also the default.
            async_io: whether io is run asynchronously (default: False)
            device: device to perform inference on (default: auto)
            viewagg_device: device to aggregate views on (default: auto)
        """
        self.pool = None
        self._threads = None
        self.threads = threads
        torch.set_num_threads(cpu_count() if self._threads is None else self._threads)
        self.pool = ThreadPoolExecutor(self._threads) if async_io else NoParallelExecutor()
        self.cfg = cfg
        self._async_io = async_io

        # Set random seed from config_files.
        np.random.seed(cfg.RNG_SEED)
        torch.manual_seed(cfg.RNG_SEED)

        _device = find_device(device)
        if _device == "cpu" and viewagg_device == "auto":
            _viewagg_device = torch.device('cpu')
        else:
            _viewagg_device = find_device(viewagg_device, flag_name="viewagg_device", min_memory=2*(2**30))

        self.batch_size = cfg.TEST.BATCH_SIZE
        cerebnet_labels_file = join(cp.FASTSURFER_ROOT, "CerebNet", "config", "CerebNet_ColorLUT.tsv")
        _cerebnet_mapper = self.pool.submit(TSVLookupTable, cerebnet_labels_file, header=True)

        self.freesurfer_color_lut_file = join(cp.FASTSURFER_ROOT, "FastSurferCNN", "config", "FreeSurferColorLUT.txt")
        fs_color_map = self.pool.submit(TSVLookupTable, self.freesurfer_color_lut_file, header=False)

        cerebnet2sagittal_lut = join(cp.FASTSURFER_ROOT, "CerebNet", "config", "CerebNet2Sagittal.json")
        cereb2cereb_sagittal = self.pool.submit(JsonColorLookupTable, cerebnet2sagittal_lut)

        cerebnet2freesurfer_lut = join(cp.FASTSURFER_ROOT, "CerebNet", "config", "CerebNet2FreeSurfer.json")
        cereb2freesurfer = self.pool.submit(JsonColorLookupTable, cerebnet2freesurfer_lut)

        _models = self._load_model(cfg)

        self.device = _device
        self.viewagg_device = _viewagg_device

        self.cerebnet_labels = _cerebnet_mapper.result().labelname2id()

        self.cereb_name2freesurfer_id = cereb2freesurfer.result().labelname2id()\
            .chain(fs_color_map.result().labelname2id())

        # the id in cereb2freesurfer is also a labelname, i.e. cereb2freesurfer is a map of Labelname2Labelname
        self.cereb2fs = self.cerebnet_labels.__reversed__()\
            .chain(self.cereb_name2freesurfer_id)

        self.cereb2cereb_sagittal = self.cerebnet_labels.__reversed__()\
            .chain(cereb2cereb_sagittal.result().labelname2id())
        self.models = {k: m.to(self.device) for k, m in _models.items()}

    @property
    def threads(self) -> int:
        return -1 if self._threads is None else self._threads

    @threads.setter
    def threads(self, threads: int):
        self._threads = threads if threads > 0 else None

    def __del__(self):
        """Make sure the pool gets shut down when the Inference object gets deleted."""
        if self.pool is not None:
            self.pool.shutdown(False)

    def _load_model(self, cfg) -> Dict[Plane, torch.nn.Module]:
        """Loads the three models per plane."""
        def __load_model(cfg: 'yacs.ConfigNode', plane: Plane) -> torch.nn.Module:
            params = {k.lower(): v for k, v in dict(cfg.MODEL).items()}
            params["plane"] = plane
            if plane == 'sagittal':
                if params['num_classes'] != params['num_classes_sag']:
                    params['num_classes'] = params['num_classes_sag']
            checkpoint_path = cfg.TEST[f'{plane.upper()}_CHECKPOINT_PATH']
            model = build_model(params)
            if not isfile(checkpoint_path):
                # if the checkpoint path is not a file, but a folder search in there for the newest checkpoint
                checkpoint_path = cp.get_checkpoint_path(checkpoint_path).pop()
            cp.load_from_checkpoint(checkpoint_path, model)
            model.eval()
            return model

        from functools import partial
        _load_model_func = partial(__load_model, cfg)
        return dict(zip(PLANES, self.pool.map(_load_model_func, PLANES)))

    @torch.no_grad()
    def _predict_single_subject(self, subject_dataset: SubjectDataset) -> Dict[Plane, List[torch.Tensor]]:
        """Predict the classes based on a SubjectDataset."""
        img_loader = DataLoader(subject_dataset, batch_size=self.batch_size, shuffle=False)
        prediction_logits = {}
        try:
            for plane in PLANES:
                subject_dataset.set_plane(plane)

                predictions = []
                from CerebNet.data_loader.data_utils import slice_lia2ras, slice_ras2lia
                for img in img_loader:
                    # CerebNet is trained on RAS+ conventions, so we need to map between lia (FastSurfer) and RAS+
                    # map LIA 2 RAS
                    img = slice_lia2ras(plane, img)
                    batch = img.to(self.device)
                    pred = self.models[plane](batch)
                    # map RAS 2 LIA
                    pred = slice_ras2lia(plane, pred)
                    pred = pred.to(device=self.viewagg_device, dtype=torch.float16)
                    predictions.append(pred)
                prediction_logits[plane] = predictions
        except RuntimeError as e:
            from FastSurferCNN.utils.common import handle_cuda_memory_exception
            handle_cuda_memory_exception(e)
            raise e
        return prediction_logits

    def _post_process_preds(self, preds: Dict[Plane, List[torch.Tensor]]) -> Dict[Plane, torch.Tensor]:
        """Permutes axes, so it has consistent sagittal, coronal, axial, channels format. Also maps
        classes of sagittal predictions into the global label space

        Args:
            preds: predicted logits.

        Returns:
            dictionary of permuted logits.
        """
        axis_permutation = {
            # a,_, s, c -> s, c, a, _
            "axial": (3, 0, 2, 1),
            # c, _, s, a -> s, c, a, _
            "coronal": (2, 3, 0, 1),
            # s, _, c, a -> s, c, a, _
            "sagittal": (0, 3, 2, 1)
        }

        def _convert(plane: Plane) -> torch.Tensor:
            pred = torch.cat(preds[plane], dim=0)
            if plane == 'sagittal':
                pred = self.cereb2cereb_sagittal.map_probs(pred, axis=1, reverse=True)
            return pred.permute(axis_permutation[plane])

        return {plane: _convert(plane) for plane in preds.keys()}

    def _view_aggregation(self, logits: Dict[Plane, torch.Tensor]) -> torch.Tensor:
        """
        Aggregate the view (axial, coronal, sagittal) into one volume and get the class of the largest probability. (argmax)

        Args:
            logits: dictionary of per plane predicted logits (axial, coronal, sagittal)

        Returns:
            Tensor of classes (of largest aggregated logits)
        """
        aggregated_logits = torch.add((logits['axial'] + logits['coronal']) * 0.4, logits['sagittal'], alpha=0.2)
        _, labels = torch.max(aggregated_logits, dim=3)
        return labels

    def _calc_segstats(self, seg_data: np.ndarray, norm_data: np.ndarray, vox_vol: float) -> 'pandas.DataFrame':
        """
            Computes volume and volume similarity
        """
        labels = [id for name, id in self.cereb_name2freesurfer_id]
        freesurfer_id2cereb_name = self.cereb_name2freesurfer_id.__reversed__()
        free_label_ids = range(freesurfer_id2cereb_name.max_label + 1, 640)
        meta_labels = ["Left Cerebellar Gray Matter", "Right Cerebellar Gray Matter", "Vermis"]
        meta_labels_short = [ml.split(" ")[0] for ml in meta_labels]
        merged_labels = {id: filter(lambda lname: lname.startwith(l), freesurfer_id2cereb_name(labels)) for id, l in zip(free_label_ids, meta_labels_short)}
        merged_labels_names = dict(zip(free_label_ids, meta_labels))

        # calculate PVE
        from FastSurferCNN.segstats import pv_calc
        table = pv_calc(seg_data, norm_data, labels, vox_vol=vox_vol,
                        threads=self.threads, patch_size=32, merged_labels=merged_labels)

        # fill the StructName field
        for i in range(len(table)):
            _id = table[i]["SegId"]
            if _id in merged_labels_names.keys():
                table[i]["StructName"] = merged_labels_names[_id]
            elif _id in freesurfer_id2cereb_name:
                table[i]["StructName"] = freesurfer_id2cereb_name[_id]
            else:
                # noinspection PyTypeChecker
                table[i]["StructName"] = "merged label " + str(_id)

        import pandas as pd
        dataframe = pd.DataFrame(table, index=np.arange(len(table)))
        dataframe = dataframe[dataframe["NVoxels"] != 0].sort_values("SegId")
        dataframe.index = np.arange(1, len(dataframe) + 1)
        return dataframe

    def _save_cerebnet_seg(self, cerebnet_seg: torch.Tensor, filename: str, bounding_box: LocalizerROI,
                           orig: nib.analyze.SpatialImage) -> 'Future[None]':
        """
        Saving the segmentations asynchronously.

        Args:
            cerebnet_seg: segmentation data
            filename: path and file name to the saved file
            bounding_box: bounding box from the full image to fill with the segmentation
            orig: file container (with header and affine) used to populate header and affine of the segmentation

        Returns:
            A Future to determine when the file was saved. Result is None.
        """
        out_data = crop_transform(cerebnet_seg,
                                  offsets=tuple(-o for o in bounding_box["offsets"]),
                                  target_shape=bounding_box["source_shape"]).cpu()

        from FastSurferCNN.data_loader.data_utils import save_image
        logger.info(f"Saving CerebNet cerebellum segmentation at {filename}")
        return self.pool.submit(save_image, orig.header, orig.affine, out_data.numpy(), filename, dtype=np.int16)

    def _get_subject_dataset(self, subject: SubjectDirectory) \
            -> Tuple[Optional[np.ndarray], Optional[str], SubjectDataset]:
        """Load and prepare input files asynchronously, then locate the cerebellum and provide a localized patch."""

        from FastSurferCNN.data_loader.data_utils import load_image

        if subject.has_attribute('cereb_statsfile'):
            if not subject.can_resolve_attribute("cereb_statsfile"):
                from FastSurferCNN.utils.parser_defaults import ALL_FLAGS
                raise ValueError(f"Cannot resolve the intended filename {subject.get_attribute('norm_name')} "
                                 f"for the cereb_statsfile, maybe specify an absolute path via "
                                 f"{ALL_FLAGS['cereb_statsfile'](dict)['flag']}.")
            if not subject.has_attribute('norm_name') or subject.fileexists_by_attribute('norm_name'):
                from FastSurferCNN.utils.parser_defaults import ALL_FLAGS
                raise ValueError(f"Cannot resolve the file name {subject.get_attribute('norm_name')} for the "
                                 f"bias field corrected image, maybe specify an absolute path via "
                                 f"{ALL_FLAGS['norm_name'](dict)['flag']} or the file does not exist.")

            norm_file = subject.filename_in_subject_folder(subject.get_attribute('norm_name'))
            # finally, load the bias field file
            norm = self.pool.submit(load_image, norm_file)
        else:
            norm_file, norm = None, None

        # localization
        if not subject.fileexists_by_attribute("aparc_aseg_segfile"):
            raise RuntimeError(f"The aparc-aseg-segmentation file '{subject.aparc_aseg_segfile}' did not exist, "
                               "please run FastSurferVINN first.")
        seg = self.pool.submit(load_image, subject.aparc_aseg_segfile)
        # create conformed image
        conf_file, is_conform, conf_img = subject.conf_name, False, None
        if subject.fileexists_by_attribute("conf_name"):
            # see if the file is 1mm
            conf_img = nib.load(conf_file)

            from FastSurferCNN.data_loader.conform import is_conform
            # is_conform only needs the header, not the data
            is_conform = is_conform(conf_img, conform_vox_size=1., verbose=False)

        if is_conform:
            # calling np.asarray here, forces the load of conf_img.dataobj into memory (which is parallel with the
            # loading of aparc_aseg, if done here)
            conf_data = np.asarray(conf_img)
        else:
            # the image is not conformed to 1mm, do this now.
            from FastSurferCNN.data_loader.data_utils import (SUPPORTED_OUTPUT_FILE_FORMATS, load_and_conform_image,
                                                              save_image)
            from nibabel.filebasedimages import FileBasedHeader as _Header
            fileext = list(filter(lambda ext: conf_file.endswith("." + ext), SUPPORTED_OUTPUT_FILE_FORMATS))
            if len(fileext) != 1:
                raise RuntimeError(f"Invalid file extension of conf_name: {conf_file}, must be one of "
                                   f"{SUPPORTED_OUTPUT_FILE_FORMATS}.")
            conf_no_fileext = conf_file[:-len(fileext[0])-1]
            if not conf_no_fileext.endswith(".1mm"):
                conf_no_fileext += ".1mm"
            # if the orig file is neither absolute nor in the subject path, use the conformed file
            if isfile(subject.orig_name):
                orig_file = subject.orig_name
            else:
                orig_file = conf_file
                logger.warn("No path to a valid orig file was given, so we might lose quality due to mutiple "
                            "chained interpolations.")

            conf_file = conf_no_fileext + "." + fileext[0]
            # conform to 1mm
            conformed = self.pool.submit(load_and_conform_image, orig_file, conform_min=False,
                                         logger=logging.getLogger(__name__ + ".conform"))

            def save_conformed_image(__conf: 'Future[Tuple[_Header, np.ndarray, np.ndarray]]') -> None:
                save_image(*__conf.result(), conf_file)

            # after conforming, save the conformed file
            conformed.add_done_callback(save_conformed_image)

            conf_header, conf_affine, conf_data = conformed.result()
            conf_img = nib.MGHImage(conf_data, conf_affine, conf_header)

        seg, seg_data = seg.result()
        subj_dset = SubjectDataset(img_org=conf_img,
                                   patch_size=self.cfg.DATA.PATCH_SIZE,
                                   slice_thickness=self.cfg.DATA.THICKNESS,
                                   primary_slice=self.cfg.DATA.PRIMARY_SLICE_DIR,
                                   brain_seg=seg)
        subj_dset.transforms = ToTensorTest()
        return (norm if norm is None else norm.result()), norm_file, subj_dset

    def run(self, subject_directories: SubjectList):
        logger.info(time.strftime("%y-%m-%d_%H:%M:%S"))

        from tqdm.contrib.logging import logging_redirect_tqdm
        start_time = time.time()
        with logging_redirect_tqdm():
            if self._async_io:
                from FastSurferCNN.utils.common import pipeline as iterate
            else:
                from FastSurferCNN.utils.common import iterate
            iter_subjects = iterate(self.pool, self._get_subject_dataset, subject_directories)
            for idx, (subject, (norm, norm_file, subject_dataset)) in tqdm(enumerate(iter_subjects),
                                                                           total=len(subject_directories),
                                                                           desc="Subject"):
                try:
                    # predict CerebNet, returns logits
                    preds = self._predict_single_subject(subject_dataset)
                    # create the folder for the output file, if it does not exist
                    _mkdir = self.pool.submit(makedirs, dirname(subject.segfile), exist_ok=True)

                    # postprocess logits (move axes, map sagittal to all classes)
                    preds_per_plane = self._post_process_preds(preds)
                    # view aggregation in logit space and find max label
                    cerebnet_seg = self._view_aggregation(preds_per_plane)

                    # map predictions into FreeSurfer Label space
                    cerebnet_seg = self.cereb2fs.map(cerebnet_seg)
                    pred_time = time.time()

                    _ = _mkdir.result()  # this is None, but synchronizes the creation of the directory
                    self._save_cerebnet_seg(cerebnet_seg, subject.segfile,
                                            subject_dataset.get_bounding_offsets(), subject_dataset.get_nibabel_img())

                    if subject.has_attribute("cereb_statsfile"):
                        # move the cereb segmentation to RAM
                        cerebnet_seg = cerebnet_seg.cpu().numpy()
                        norm, norm_data = norm.result()  # wait for the loading of the bias field file here
                        # vox_vol = np.prod(norm.header.get_zooms()).item()  # CerebNet always has vox_vol 1
                        df = self._calc_segstats(cerebnet_seg, norm, vox_vol=1.)
                        from FastSurferCNN.segstats import write_statsfile
                        # in batch processing, we are finished with this subject and the output of this data can be
                        # outsourced to a different process
                        self.pool.submit(write_statsfile, subject.cereb_statsfile, df, vox_vol=1.,
                                         segfile=subject.segfile, normfile=norm_file, lut=self.freesurfer_color_lut_file)

                    logger.info(f"Subject {idx + 1}/{len(subject_directories)} with id '{subject.id}' "
                                f"processed in {pred_time - start_time :.2f} sec.")
                except Exception as e:
                    logger.exception(e)
                    return "\n".join(map(str, e.args))
                else:
                    start_time = time.time()

        return 0
