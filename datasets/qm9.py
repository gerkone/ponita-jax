from typing import List, Callable
import os
import requests
import zipfile
import pickle
import pandas as pd
import numpy as np
import jax.numpy as jnp
import torch

from torch.utils.data import Dataset
from rdkit import Chem
from rdkit.Chem import rdchem
from tqdm import tqdm


class QM9Dataset(Dataset):
    # Conversion factors for targets
    HAR2EV = 27.211386246
    KCALMOL2EV = 0.04336414
    TOTAL_SIZE = 130831  # Total size of the dataset
    TRAIN_SIZE = 110000
    VAL_SIZE = 10000  # 110000:120000 for validation
    TEST_SIZE = 10831  # Remaining for test
    TARGETS = ['mu', 'alpha', 'homo', 'lumo', 'gap', 'r2', 'zpve', 'U0', 'U', 'H', 'G', 'Cv', 'U0_atom', 'U_atom', 'H_atom', 'G_atom', 'A', 'B', 'C']
    UNCHARACTERIZED_URL = 'https://ndownloader.figshare.com/files/3195404'

    def __init__(self, root='./datasets/qm9_dataset', sdf_file='gdb9.sdf', csv_file='gdb9.sdf.csv', target=None, split=None):
        self.root = root
        self.sdf_file = os.path.join(root, sdf_file)
        self.csv_file = os.path.join(root, csv_file)
        self.processed_file = os.path.join(root, 'processed_qm9_data.pkl')
        self.uncharacterized_file = os.path.join(root, 'uncharacterized.txt')
        self.qm9_url = 'https://deepchemdata.s3-us-west-1.amazonaws.com/datasets/molnet_publish/qm9.zip'
        self.target_index = None if target is None else self.TARGETS.index(target)
        self.split = split  # Split can be 'train', 'val', or 'test'

        if not os.path.exists(self.root):
            os.makedirs(self.root, exist_ok=True)

        if os.path.isfile(self.processed_file):
            print("Loading processed data...")
            with open(self.processed_file, 'rb') as f:
                self.data = pickle.load(f)
        else:
            self.download_uncharacterized()
            self.ensure_data_downloaded()
            print("Processing data from scratch...")
            self.data = self.process()
            with open(self.processed_file, 'wb') as f:
                pickle.dump(self.data, f)

        if split:
            self.apply_split()
        
        self.dataset_to_pytorch()

    def apply_split(self):
        # Create the split based on the predefined sizes (seed used by DimeNet)
        random_state = np.random.RandomState(seed=42)
        perm = random_state.permutation(np.arange(self.TOTAL_SIZE))
        train_idx, val_idx, test_idx = perm[:self.TRAIN_SIZE], perm[self.TRAIN_SIZE:self.TRAIN_SIZE + self.VAL_SIZE], perm[self.TRAIN_SIZE + self.VAL_SIZE:]

        if self.split == 'train':
            self.data = [self.data[i] for i in train_idx]
        elif self.split == 'val':
            self.data = [self.data[i] for i in val_idx]
        elif self.split == 'test':
            self.data = [self.data[i] for i in test_idx]
        else:
            raise ValueError("Split must be 'train', 'val', or 'test'.")
        
    def dataset_to_pytorch(self):
        for i in range(len(self.data)):
            self.data[i]['x'] = self.data[i]['x'].astype(np.float32)
            self.data[i]['y'] = self.data[i]['y'].astype(np.float32)
            self.data[i]['pos'] = self.data[i]['pos'].astype(np.float32)
            self.data[i]['edge_attr'] = self.data[i]['edge_attr'].astype(np.float32)
            self.data[i]['edge_index'] = self.data[i]['edge_index'].astype(int)

    def download_uncharacterized(self):
        """Download the uncharacterized.txt file."""
        if not os.path.isfile(self.uncharacterized_file):
            print("Downloading uncharacterized.txt...")
            response = requests.get(self.UNCHARACTERIZED_URL)
            response.raise_for_status()  # Ensure the request was successful
            with open(self.uncharacterized_file, 'wb') as f:
                f.write(response.content)

    def read_uncharacterized_indices(self):
        """Read indices from uncharacterized.txt file."""
        # with open(self.uncharacterized_file, 'r') as file:
            # indices = [int(line.strip()) - 1 for line in file if line.strip().isdigit()]  # Adjusting indices to 0-based
        with open(self.uncharacterized_file, 'r') as f:
            skip = [int(x.split()[0]) - 1 for x in f.read().split('\n')[9:-2]]
        return set(skip)

    def download_file(self, url, filename):
        local_filename = os.path.join(self.root, filename)
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(local_filename, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return local_filename

    def extract_zip(self, file_path):
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(self.root)
        print(f"Extracted to {self.root}")

    def ensure_data_downloaded(self):
        if not os.path.isfile(self.sdf_file) or not os.path.isfile(self.csv_file):
            print(f"SDF or CSV file not found, downloading and extracting QM9 dataset...")
            zip_file_path = self.download_file(self.qm9_url, 'qm9.zip')
            self.extract_zip(zip_file_path)
        else:
            print("SDF and CSV files found, no need to download.")

    def process(self):
        # suppl = Chem.SDMolSupplier(self.raw_paths[0], removeHs=False, sanitize=False)

        suppl = Chem.SDMolSupplier(self.sdf_file, removeHs=False, sanitize=False)
        df = pd.read_csv(self.csv_file)
        raw_targets = df.iloc[:, 1:].values
        raw_targets = raw_targets.astype(np.float32)

        rearranged_targets = np.concatenate([raw_targets[:, 3:], raw_targets[:, :3]], axis=1)
        conversion_factors = np.array([
            1., 1., self.HAR2EV, self.HAR2EV, self.HAR2EV, 1., self.HAR2EV, self.HAR2EV, self.HAR2EV,
            self.HAR2EV, self.HAR2EV, 1., self.KCALMOL2EV, self.KCALMOL2EV, self.KCALMOL2EV,
            self.KCALMOL2EV, 1., 1., 1.
        ], dtype=np.float32)

        targets = rearranged_targets * conversion_factors

        atom_types = {1: 0, 6: 1, 7: 2, 8: 3, 9: 4}
        data_list = []

        skip_indices = self.read_uncharacterized_indices()


        for i, mol in enumerate(tqdm(suppl, desc="Processing Molecules")):
            if mol is None or i in skip_indices:  # Skip uncharacterized molecules
                continue
            # if mol is None: continue
            num_atoms = mol.GetNumAtoms()
            pos = np.array([mol.GetConformer().GetAtomPosition(j) for j in range(num_atoms)], dtype=np.float32)
            x = np.zeros((num_atoms, len(atom_types)), dtype=bool)  # one-hot encoding

            for j in range(num_atoms):
                atom = mol.GetAtomWithIdx(j)
                x[j, atom_types[atom.GetAtomicNum()]] = 1

            y = targets[i]
            name = mol.GetProp('_Name')
            smiles = Chem.MolToSmiles(mol)

            # Initialize lists for edge indices and attributes
            edge_indices = []
            edge_attrs = []

            for bond in mol.GetBonds():
                start, end = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()

                # Bond type one-hot encoding: single, double, triple, aromatic
                bond_type = [0, 0, 0, 0]
                if bond.GetBondType() == rdchem.BondType.SINGLE:
                    bond_type[0] = 1
                elif bond.GetBondType() == rdchem.BondType.DOUBLE:
                    bond_type[1] = 1
                elif bond.GetBondType() == rdchem.BondType.TRIPLE:
                    bond_type[2] = 1
                elif bond.GetBondType() == rdchem.BondType.AROMATIC:
                    bond_type[3] = 1

                edge_indices.append((start, end))
                edge_indices.append((end, start))  # Add reverse direction for undirected graph

                edge_attrs += [bond_type, bond_type]  # Same attributes for both directions

            # Convert edge data to tensors
            edge_index = np.array(edge_indices, dtype=int).T
            edge_attr = np.array(edge_attrs, dtype=bool)

            # Sorting edge_index by source node indices
            sort_indices = np.lexsort((edge_index[0, :], edge_index[1, :]))
            edge_index = edge_index[:, sort_indices]
            edge_attr = edge_attr[sort_indices]

            data_list.append({
                'pos': pos,
                'x': x,
                'y': y,
                'edge_index': edge_index,
                'edge_attr': edge_attr,
                'name': name,
                'smiles': smiles,
                'idx': i
            })

        return data_list

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        item = self.data[idx]
        if self.target_index is not None:
            if len(item['y']) > 1:
                item['y'] = item['y'][self.target_index:self.target_index+1]
            else:
                # The item is already updated
                pass
        return item

    def calc_stats(self):
        ys = np.array([data["y"].item() for data in self])
        mean = np.mean(ys)
        mad = np.mean(np.abs(ys - mean))
        return mean, mad

    def top_n_nodes(self, n: int) -> List[int]:
        """Returns the largest n nodes in the dataset."""
        return [int(k) for k in torch.topk(torch.tensor([a["pos"].shape[0] for a in self.data]), n)[0]]

    def top_n_edges(self, n: int) -> List[int]:
        """Returns the largest n edge in the dataset."""
        return [int(k) for k in torch.topk(torch.tensor([a["edge_index"].shape[1] for a in self.data]), n)[0]]


def collate_fn(batch):
    pos, x, y, batch_idx, edge_index_batch, edge_attr_batch, ptr = [], [], [], [], [], [], []
    cum_nodes = 0
    ptr.append(0)
    for i, item in enumerate(batch):
        num_nodes = item['x'].shape[0]
        pos.append(item['pos'])
        x.append(item['x'])
        y.append(item['y'])
        batch_idx.extend([i] * num_nodes)
        edge_index = item['edge_index'] + cum_nodes  # Offset node indices
        edge_index_batch.append(edge_index)
        edge_attr_batch.append(item['edge_attr'])
        cum_nodes += num_nodes
        ptr.append(cum_nodes)
    pos = np.concatenate(pos, axis=0)
    x = np.concatenate(x, axis=0)
    y = np.stack(y, axis=0)
    batch_idx = np.array(batch_idx, dtype=jnp.int32)
    ptr = np.array(ptr, dtype=jnp.int32)
    edge_index = np.concatenate(edge_index_batch, axis=1)
    edge_attr = np.concatenate(edge_attr_batch, axis=0)
    return {'pos': pos, 'x': x, 'y': y, 'batch': batch_idx, 'ptr': ptr, 'edge_index': edge_index,'edge_attr': edge_attr}


def QM9GraphTransform(
    max_batch_nodes: int,
    max_batch_edges: int,
) -> Callable:
    def _qm9_transform(data):
        pos = jnp.array(data["pos"])
        x = jnp.array(data["x"])
        edge_attr = jnp.array(data["edge_attr"])
        edge_index = jnp.array(data["edge_index"], dtype=jnp.int32)
        graph_index = jnp.array(data["batch"], dtype=jnp.int32)
        # ptr = jnp.array(data["ptr"])
        # n_node = jnp.diff(ptr)
        # n_edge = jnp.diff(jnp.sum(edge_index[0][:, None] < ptr, axis=0))
        n_nodes = x.shape[0]
        n_edges = edge_index.shape[1]
        
        # pad for jax static shapes (0.8 as (un)safety factor)
        n_node_pad = int(0.8 * max_batch_nodes - n_nodes + 1)
        n_edge_pad = int(0.8 * max_batch_edges - n_edges + 1)
        node_pad = ((0, n_node_pad), (0, 0))
        edge_pad = ((0, n_edge_pad), (0, 0))
        edge_index_pad = ((0, 0), (0, n_edge_pad))
        pos = jnp.pad(pos, node_pad)
        x = jnp.pad(x, node_pad)
        edge_index = jnp.pad(edge_index, edge_index_pad, constant_values=edge_index.max() + 1)
        edge_attr = jnp.pad(edge_attr, edge_pad)
        # most important part: batch indices
        graph_index = jnp.append(graph_index,  jnp.array(n_node_pad * [graph_index[-1] + 1]))
        padding_mask = jnp.append(jnp.ones_like(data["y"]), 0)
        
        # also pad targets (graph task)
        target = jnp.append(jnp.array(data["y"]), 0)

        graph = {
            "pos": pos,
            "x": x,
            "edge_index": edge_index,
            "edge_attr": edge_attr,
            "graph_index": graph_index,
            "y": target,
            "padding_mask": padding_mask
        }
        return graph

    return _qm9_transform


if __name__ == '__main__':
    from torch.utils.data import DataLoader
    import time

    batch_size = 96
    dataset = QM9Dataset(target='alpha', split='train')
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=False, collate_fn=collate_fn, pin_memory=True, num_workers=0)

    t0 = time.time()
    for batch in tqdm(dataloader):
        x, y, pos, edge_index, edge_attr = batch['x'], batch['y'], batch['pos'], batch['edge_index'], batch['edge_attr']
    t1 = time.time()
    print(t1-t0)