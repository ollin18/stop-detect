import numpy as np
import bisect
import polars as pl
from sklearn.neighbors import BallTree
from scipy.spatial import ConvexHull, QhullError
from infomap import Infomap
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor


def pass_func(input, **kwargs):
    return input

def euclidean(x1, y1, x2, y2):
    return np.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)

def median(values):
    return np.median(values)

def insert_ordered(arr, elem):
    bisect.insort(arr, elem)

def finalize_group(stat_coords, event_map, stop_points_lat, stop_points_lon, i0, i, j):
    stat_coords.append([np.median(stop_points_lat), np.median(stop_points_lon)])
    for idx in range(i0, i):
        event_map[idx] = j
    return j + 1

def query_neighbors(coords, r2, distance_metric="haversine", weighted=False, num_threads=1):
    """Build a network from a set of points and a threshold distance."""
    counts = coords.select("count").to_numpy().flatten().tolist()
    # Extract the coordinates from the Polars DataFrame
    coords_list = np.array(coords.select("coords").to_numpy().tolist()).reshape(-1, 2)

    # Convert to radians if using haversine distance
    if distance_metric == "haversine":
        coords_list = np.radians(coords_list)
        r2 = r2 / 6371000  # Earth radius in meters

    # Build the BallTree for neighbor queries
    tree = BallTree(coords_list, metric=distance_metric)

    # Function to query a chunk of coordinates
    def query_chunk(index_chunk):
        index, chunk = index_chunk
        result = tree.query_radius(chunk, r=r2, return_distance=weighted)
        return index, result

    # Determine the number of chunks
    num_chunks = num_threads if len(coords_list) >= num_threads else len(coords_list)
    # Split coordinates into chunks and keep track of indices
    chunks = [(i, chunk) for i, chunk in enumerate(np.array_split(coords_list, num_chunks))]

    # Use ThreadPoolExecutor to process chunks in parallel
    with ThreadPoolExecutor(max_workers=num_chunks) as executor:
        results = list(executor.map(query_chunk, chunks))

    # Sort results based on the original indices to preserve order
    results.sort(key=lambda x: x[0])
    combined_results = np.concatenate([result for _, result in results])

    return combined_results, counts

def infomap_communities(node_idx_neighbors, node_idx_distances, counts, weight_exponent, distance_metric, verbose):
    """Two-level partition of single-layer network with Infomap."""

    progress = tqdm if verbose else pass_func
    network = Infomap("--two-level" + (" --silent" if not verbose else ""))

    # Map nodes
    name_map, name_map_inverse, singleton_nodes = {}, {}, []
    infomap_idx = 0

    for n, neighbors in progress(enumerate(node_idx_neighbors), total=len(node_idx_neighbors)):
        if len(neighbors) > 1:
            network.addNode(infomap_idx)
            name_map_inverse[infomap_idx] = n
            name_map[n] = infomap_idx
            infomap_idx += 1
        else:
            singleton_nodes.append(n)

    # Add edges
    add_edges(network, node_idx_neighbors, node_idx_distances, name_map, counts, weight_exponent, distance_metric, verbose)

    # Run Infomap
    if len(name_map) > 0:
        network.run()
        partition = {name_map_inverse[infomap_idx]: module for infomap_idx, module in network.modules}
    else:
        partition = {}

    if verbose:
        print(f"Found {len(set(partition.values()))-1} stop locations")

    return partition, singleton_nodes

def add_edges(network, node_idx_neighbors, node_idx_distances, name_map, counts, weight_exponent, distance_metric, verbose):
    """Add edges to the Infomap network."""

    progress = tqdm if verbose else pass_func
    n_edges = 0

    for node, neighbors in progress(enumerate(node_idx_neighbors), total=len(node_idx_neighbors)):
        if node_idx_distances is None:
            for neighbor in neighbors[neighbors > node]:
                network.addLink(name_map[node], name_map[neighbor], max(counts[node], counts[neighbor]))
                n_edges += 1
        else:
            for neighbor, distance in zip(neighbors[neighbors > node], node_idx_distances[neighbors > node]):
                if distance_metric == "haversine":
                    distance *= 6371000
                network.addLink(
                    name_map[node], name_map[neighbor], max(counts[node], counts[neighbor]) * distance ** (-weight_exponent)
                )
                n_edges += 1

    if verbose:
        print(f"    --> added {n_edges} edges")

def label_network(node_idx_neighbors, node_idx_distances, counts, weight_exponent, label_singleton, distance_metric, verbose):
    """Infer infomap clusters from distance matrix and link distance threshold."""

    partition, singleton_nodes = infomap_communities(node_idx_neighbors, node_idx_distances, counts, weight_exponent, distance_metric, verbose)

    if label_singleton:
        max_label = max(partition.values(), default=-1)
        partition.update({n: i for i, n in enumerate(singleton_nodes, start=max_label + 1)})

    # Convert partition dictionary to a label array
    return np.array([partition.get(n, -1) for n in range(len(node_idx_neighbors))])

def max_pdist(points):
    """Calculate the maximum pairwise distance in a set of points."""

    c = points.shape[0]
    result = np.zeros((c * (c - 1) // 2,), dtype=np.float64)
    vec_idx = 0

    for idx in range(0, c - 1):
        ref = points[idx]
        temp = np.linalg.norm(points[idx + 1:c, :] - ref, axis=1)
        result[vec_idx: vec_idx + temp.shape[0]] = temp
        vec_idx += temp.shape[0]

    return max(result)

def convex_hull(points, to_return="points"):
    """Return the convex hull of a collection of points."""

    try:
        hull = ConvexHull(points)
        return points[hull.vertices, :]
    except QhullError:
        c = points.mean(0)
        l = 5e-5 if points.shape[0] == 1 else max_pdist(points)
        return np.vstack([
            c + np.array([-l / 2, -l / 2]),  # bottom left
            c + np.array([l / 2, -l / 2]),   # bottom right
            c + np.array([l / 2, l / 2]),    # top right
            c + np.array([-l / 2, l / 2])    # top left
        ])

##################################################################
##################################################################
##################################################################
##################################################################
##################################################################
##################################################################
def haversine(lat1, lon1, lat2, lon2):
    # Ensure input arrays are numpy arrays for vectorized operations
    lat1, lon1, lat2, lon2 = map(np.asarray, [lat1, lon1, lat2, lon2])

    # Convert latitude and longitude from degrees to radians
    lat1_rad = np.radians(lat1)
    lon1_rad = np.radians(lon1)
    lat2_rad = np.radians(lat2)
    lon2_rad = np.radians(lon2)

    # Haversine formula
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon / 2.0) ** 2
    c = 2 * np.arcsin(np.sqrt(a))

    # Radius of Earth in meters
    R = 6371000
    distance = R * c

    return distance

def get_stationary_events(input_df, r_C, min_size, min_staying_time, max_staying_time, distance_metric):
    coords = input_df.select(['uid', 'latitude', 'longitude', 'timestamp'])

    if coords.is_empty():
        return [], []

    # Determine distance function
    if distance_metric == "haversine":
        distance_function = haversine
    elif distance_metric == "euclidean":
        distance_function = lambda lat1, lon1, lat2, lon2: np.sqrt((lat2 - lat1) ** 2 + (lon2 - lon1) ** 2)
    else:
        raise ValueError("Unsupported distance metric")

    # Calculate distances between consecutive points
    coords = coords.with_columns([
        distance_function(
            pl.col('latitude').shift(-1), pl.col('longitude').shift(-1),
            pl.col('latitude'), pl.col('longitude')
        ).alias('distance'),
        (pl.col('timestamp').shift(-1) - pl.col('timestamp')).alias('time_diff')
    ])

    # Create a mask for stationary events based on thresholds
    coords = coords.with_columns([
        (pl.col('distance') <= r_C).alias('within_radius'),
        (pl.col('time_diff').is_null() | (pl.col('time_diff') <= max_staying_time)).alias('within_time')
    ])

    # Find clusters of points that meet the stationary criteria
    coords = coords.with_columns([
    (pl.col('within_radius') & pl.col('within_time')).alias('stationary')
])

    coords = coords.with_columns([
        (pl.col('stationary') & (~pl.col('stationary').shift(1, fill_value=False))).cast(pl.Int32).alias('event_change')
    ])

    # Create event IDs by cumulatively summing up the transitions
    coords = coords.with_columns([
        (pl.col('event_change').cum_sum().alias('event_id'))
    ])

    # Filter events based on min_size and min_staying_time
    event_stats = coords.group_by('event_id').agg([
        pl.col('event_id').count().alias('event_size'),
        pl.col('time_diff').sum().alias('total_time')
    ]).filter(
        (pl.col('event_size') >= min_size) & (pl.col('total_time') >= min_staying_time)
    )

    # Keep only valid events
    valid_event_ids = event_stats['event_id'].to_list()

    coords = coords.with_columns([
        pl.when(pl.col('event_id').is_in(valid_event_ids))
        .then(pl.col('event_id'))
        .otherwise(-1)
        .alias('event_id')
    ])

    coords = coords.with_columns(stat_coords=np.array(coords.select(['latitude', 'longitude']).cast(pl.Float64)))

    out = coords.select(
        pl.col("uid"),
        pl.col("event_id").cast(pl.Int64).alias("stop_events"),
        pl.col("stat_coords").alias("event_maps"),
        pl.col('timestamp')
    )

    return out