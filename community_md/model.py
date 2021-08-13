import jax
import jax.numpy as np
import numpy as onp
from jax import random
import pickle
import networkx as nx
import jax.scipy as jscipy
import matplotlib.pyplot as plt
import numpy as onp
from jax import jit, grad, vmap, value_and_grad
from jax import lax
from jax import ops
from typing import Dict

from jax.config import config

config.update("jax_enable_x64", True)

from jax_md import space, smap, energy, minimize, quantity, simulate, partition

from functools import partial
import time

f32 = np.float32
f64 = np.float64

from tqdm import tqdm

import matplotlib
import matplotlib.pyplot as plt


def format_plot(x, y):
    plt.grid(True)
    plt.xlabel(x, fontsize=20)
    plt.ylabel(y, fontsize=20)


def finalize_plot(shape=(1, 0.7)):
    plt.gcf().set_size_inches(
        shape[0] * 1.5 * plt.gcf().get_size_inches()[1],
        shape[1] * 1.5 * plt.gcf().get_size_inches()[1])
    plt.tight_layout()


# def calculate_bond_data(displacement_or_metric, R, dr_cutoff, species=None):
#     if (not (species is None)):
#         assert (False)
#
#     metric = space.map_product(space.canonicalize_displacement_or_metric(displacement))
#     dr = metric(R, R)
#
#     dr_include = np.triu(np.where(dr < dr_cutoff, 1, 0)) - np.eye(R.shape[0], dtype=np.int32)
#     index_list = np.dstack(np.meshgrid(np.arange(N), np.arange(N), indexing='ij'))
#
#     i_s = np.where(dr_include == 1, index_list[:, :, 0], -1).flatten()
#     j_s = np.where(dr_include == 1, index_list[:, :, 1], -1).flatten()
#     ij_s = np.transpose(np.array([i_s, j_s))
#
#     bonds = ij_s[(ij_s != np.array([-1, -1]))[:, 1]]
#     lengths = dr.flatten()[(ij_s != np.array([-1, -1]))[:, 1]]
#
#     return bonds, lengths


def plot_system(R, box_size, species=None, ms=20):
    R_plt = onp.array(R)

    if (species is None):
        plt.plot(R_plt[:, 0], R_plt[:, 1], 'o', markersize=ms)
    else:
        for ii in range(np.amax(species) + 1):
            Rtemp = R_plt[species == ii]
            plt.plot(Rtemp[:, 0], Rtemp[:, 1], 'o', markersize=ms)

    plt.xlim([0, box_size])
    plt.ylim([0, box_size])
    plt.xticks([], [])
    plt.yticks([], [])

    finalize_plot((1, 1))


def opposite_dict(d: Dict):
    o = {}
    for k, v in d.items():
        o[v] = k
    return o


def readjust_graph(G: nx.Graph, suggestions={}):
    nodes = list(G.nodes)
    index_map = suggestions
    reverse_map = opposite_dict(suggestions)
    for i, x in enumerate(nodes):
        if x not in reverse_map.keys():
            index_map[i] = x
            reverse_map[x] = i

    new_edge_list = []
    for a, b in G.edges:
        new_edge_list.append((reverse_map[a], reverse_map[b]))
    new_G = nx.Graph()
    new_G.add_edges_from(new_edge_list)
    return new_G, index_map


def unindex_graph(G: nx.Graph, index_map):
    nodes = list(G.nodes)
    old_edges = []
    for a, b in G.edges:
        old_edges.append((index_map[a], index_map[b]))
    old_G = nx.Graph()
    old_G.add_edges_from(old_edges)
    return old_G


class MolecularCommunities:
    def __init__(self, key, G: nx.Graph, dim=2, box_size=1000, minimization_steps=5200, pos: np.DeviceArray=None):
        """
        Initialize CMD for a graph
        :param key: jax prng key
        :param G: networkx graph
        :param dim: embedding dimensions
        :param box_size: MD box
        :param minimization_steps: epochs for MD simulation
        """
        self.key, self.split = random.split(key)
        self.dim = dim
        self.box_size = box_size
        self.G, self.G_reindex = readjust_graph(G)

        if pos is None:
            self.R = random.uniform(self.split, (G.number_of_nodes(), dim), minval=0, maxval=box_size, dtype=np.float64)
        else:
            self.R = pos
        self.node_border_distance =  5 #@param {type:"number"}
        self.bond_border_distance = 3 #@param {type:"number"}
        self.bond_intensity = 20 #@param {type:"number"}
        self.custom_morse = partial(energy.morse, sigma=1., epsilon=10., alpha=1.)
        self.displacement, self.shift = space.periodic(box_size)

        self.r_cutoff = box_size/2.
        self.dr_threshold = box_size/8.0
        self.bonds = np.array(list(G.edges()))
        self.minimization_steps = minimization_steps
        self.embeddings = None

    def transform(self, edge_list, iters=100):
        # Get original nodes
        old_G = unindex_graph(self.G, self.G_reindex)
        # keep track of new nodes
        current_nodes = list(old_G)
        new_nodes = []
        new_bonds = []
        # Find nodes not present
        for n1, n2 in edge_list:
            contain_new_node = False
            if n1 not in current_nodes:
                new_nodes.append(n1)
                contain_new_node = True
            if n2 not in current_nodes:
                new_nodes.append(n2)
                contain_new_node = True
            if contain_new_node:
                new_bonds.append((n1, n2))
        # add new nodes
        old_G.add_edges_from(edge_list)
        # remap
        old_G, index_mapping = readjust_graph(old_G, suggestions=self.G_reindex)
        # transform new nodes
        embedding_list = []
        for _ in range(iters):
            self.key, self.split = random.split(self.key)
            new_positions = random.uniform(self.split, (len(new_nodes), self.dim), minval=0, maxval=self.box_size, dtype=np.float64)
            # TODO check the node numbers and positions in the embeddings.
            # Recreate embeddings? Embedding to node map?
            new_embeddings = np.vstack((self.embeddings, new_positions))
            bond_en = self.get_bond_energy_fn(self.displacement, new_bonds)
            calculated_embeddings, energy = self.run_minimization(bond_en, new_embeddings, self.shift, num_steps=self.minimization_steps)
            embedding_list.append(onp.arrray(calculated_embeddings))
            del calculated_embeddings
        # filter new node embeddings


        # Create random positions for new nodes
        G_new = self.G.copy()
        G_new.add_edges_from(edge_list)
        embedding_list = []
        for _ in range(iters):
            self.key, self.split = random.split(self.key)
            new_positions = random.uniform(self.split, (len(new_nodes), self.dim), minval=0, maxval=self.box_size, dtype=np.float64)
            # TODO check the node numbers and positions in the embeddings.
            # Recreate embeddings? Embedding to node map?
            new_embeddings = np.vstack((self.embeddings, new_positions))
            bond_en = self.get_bond_energy_fn(self.displacement, new_bonds)
            calculated_embeddings, energy = self.run_minimization(bond_en, new_embeddings, self.shift, num_steps=self.minimization_steps)
            embedding_list.append(onp.arrray(calculated_embeddings))
            del calculated_embeddings
        final_embedding = np.average(embedding_list, axis=0)
        labeled_embeddings = {}
        for i, emb in enumerate(final_embedding):
            labeled_embeddings[index_mapping[i]] = emb
        # get the new embeddings only
        # new_final_embeddings = final_embedding[len(new_nodes):]
        return labeled_embeddings

    def train(self):
        bond_en = self.get_bond_energy_fn(self.displacement, self.bonds)
        energy_fn, neighbor_fn = self.get_energy_fn(self.displacement, self.bonds, self.box_size, self.r_cutoff, self.dr_threshold)
        R_final, max_energy = self.run_minimization(bond_en, self.R, self.shift, num_steps=self.minimization_steps)
        # print(max_energy)
        self.embeddings = R_final
        return R_final, max_energy

    def linear_energy(self, dr, **kwargs):
        U = dr * 2 - 20
        return np.array(U, dtype=dr.dtype)

    def log_energy(self, dr, stretch=None, intensity=None, **kwargs):
        if stretch is None:
            stretch = self.bond_border_distance
        if intensity is None:
            intensity = self.bond_intensity
        U = - intensity * dr
        # U = - intensity * np.log(dr/bond_border_distance)
        return np.array(U, dtype=dr.dtype)

    def lj_energy(self, dr, d0=30, **kwargs):
        U = dr ** 2 + energy.lennard_jones(dr, sigma=d0)
        return np.array(U, dtype=dr.dtype)

    def custom_energy(self, dr, stretch=None, **kwargs):
        if stretch is None:
            stretch = self.node_border_distance
        return self.custom_morse(dr/stretch)

    def get_bond_energy_fn(self, displacement_or_metric, bonds, bond_type=None):
        return smap.bond(self.lj_energy,
                         space.canonicalize_displacement_or_metric(displacement_or_metric),
                         bonds
                         )

    def get_energy_fn(self, displacement, bonds, box_size, r_cutoff, dr_threshold):
        bond_energy_fn = self.get_bond_energy_fn(displacement, np.array(bonds))
        neighbor_fn = partition.neighbor_list(displacement,
                        box_size,
                        r_cutoff,
                        dr_threshold)
        energy_fn_all = smap.pair_neighbor_list(self.custom_energy,
                        space.canonicalize_displacement_or_metric(displacement))

        def energy_fn(R, neighbor, energy_fn_all, energy_fn_bond):
            return energy_fn_all(R, neighbor=neighbor) + energy_fn_bond(R)

        m_energy_fn = partial(energy_fn, energy_fn_all=energy_fn_all, energy_fn_bond=bond_energy_fn)

        return m_energy_fn, neighbor_fn

    def run_minimization(self, energy_fn, R_init, shift, num_steps=5000):
        dt_start = 0.001
        dt_max   = 0.004
        init, apply=minimize.fire_descent(jit(energy_fn),shift,dt_start=dt_start,dt_max=dt_max)
        apply = jit(apply)

        @jit
        def scan_fn(state, i):
            return apply(state), 0.

        state = init(R_init)
        state, _ = lax.scan(scan_fn,state,np.arange(num_steps))

        return state.position, np.amax(np.abs(-grad(energy_fn)(state.position)))

    def minimization_loop(self, energy_fn, neighbor_fn, R, shift, epochs, steps_per_epoch):
        nbrs = None # neighbor_fn(R)
        energy_fn_nbrs = None
        R_history = [R]
        force_history = [0]
        R_current = R
        max_force = None
        for _ in tqdm(range(epochs)):
            if nbrs is None or nbrs.did_buffer_overflow:
                nbrs = neighbor_fn(R)
            else:
                nbrs = neighbor_fn(R, nbrs)
            energy_fn_nbrs = partial(energy_fn, neighbor=nbrs)
            R_current, max_force = self.run_minimization(energy_fn_nbrs, R_current, shift, steps_per_epoch)
            R_history.append(R)
            force_history.append(max_force)

        return R_current, max_force, R_history, force_history

