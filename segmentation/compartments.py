import numpy as np
import pandas as pd

from skimage import img_as_bool, img_as_float, img_as_ubyte
from skimage.filters import threshold_local
from skimage.exposure import rescale_intensity
from skimage.morphology import disk
from skimage.measure import find_contours, label
from skimage.util import invert
from skimage.filters.rank import entropy
from shapely.geometry import Polygon

from sklearn.cluster import DBSCAN
from sklearn.preprocessing import StandardScaler

from cached import CachedImageFile, cached_step
from measurements import concentric, simple_polygon
from logger import get_logger

log = get_logger(name='segmentation-compartment')


def segment_compartments_from_holes(image):
    data = rescale_intensity(image, out_range=(0, np.iinfo(np.uint16).max))
    data = invert(data)
    data = img_as_ubyte(rescale_intensity(img_as_float(data), out_range=(0, 1)))

    entr_img = img_as_ubyte(rescale_intensity(entropy(data, disk(30)), out_range=(0, 1)))
    entr_img = invert(entr_img)

    segmented_polygons = list()
    for offst in np.arange(start=1, stop=300, step=1):
        local_thresh = threshold_local(entr_img, block_size=35, offset=offst)
        binary_local = img_as_bool(local_thresh)
        label_image = label(binary_local)

        # store all contours found
        contours = find_contours(label_image, 0.9)
        log.debug(f"Number of blobs found at offset {offst} ={len(contours):d}. "
                  f"Local threshold stats: min={np.min(local_thresh):4.1f} max={np.max(local_thresh):4.1f}")

        for contr in contours:
            if len(contr) < 3:
                continue
            # as the find_contours function returns values in (row, column) form,
            # we need to flip the columns to match (x, y) = (col, row)
            pol = Polygon(np.fliplr(contr))
            segmented_polygons.append({
                'offset':   offst,
                'boundary': pol
                })

    return segmented_polygons


def segment_zstack(image_structure: CachedImageFile, channel=0, frame=0) -> pd.DataFrame:
    # iterate through z in the z-stacks and segment compartments
    out = pd.DataFrame()
    for z in image_structure.zstacks:
        # get the image based on the metadata given index
        ix = image_structure.ix_at(c=channel, z=z, t=frame)
        if ix is None:
            continue
        img_md = image_structure.image(ix)

        log.debug(f"Processing image at z={z}.")

        cdir = image_structure.cache_path
        compartments = cached_step(f"z{img_md.z}c{img_md.channel}t{img_md.frame}-bags.obj",
                                   segment_compartments_from_holes, img_md.image,
                                   cache_folder=cdir)
        df = pd.DataFrame(compartments)
        df.loc[:, 'z'] = z
        df = (df
              .pipe(simple_polygon)
              .pipe(concentric)
              )
        out = out.append(df)
    return out.reset_index(drop=True)


def cluster_by_centroid(df: pd.DataFrame, eps=0.01, min_samples=10) -> pd.DataFrame:
    X = df['boundary'].apply(lambda b: b.centroid.coords[:]).values
    X = np.array([item for sublist in X for item in sublist])
    X = StandardScaler().fit_transform(X)

    # Compute DBSCAN
    db = DBSCAN(eps=eps, min_samples=min_samples).fit(X)

    return df.assign(cluster=db.labels_)
