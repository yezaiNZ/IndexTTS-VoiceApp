"""Pinned files required by the normal (non-Qwen) IndexTTS 2.5 runtime.

Hashes are the upstream HF LFS hashes, cross-checked against ModelScope.
Do not download whole auxiliary repositories: w2v's conformer checkpoint
duplicates the safetensors weights and is not used by this application.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelFile:
    path: str
    repo: str
    revision: str
    remote_path: str
    size: int
    sha256: str
    modelscope_repo: str | None = None


MAIN_REPO = "IndexTeam/IndexTTS-2.5"
MAIN_REVISION = "c39ce5ba981572cb187443877ff559dfb246ce63"
MAIN_FILES = (
    (
        "LICENSE",
        10553,
        "cc7da9ea0f8a97ef15ab3bf0389e636ce79ffca1aef5489520796ac87d87a87b",
    ),
    (
        "config.yaml",
        2860,
        "18adf417be3e8f5e2e48e30f7420c719170a6870619436250f360d626877870e",
    ),
    (
        "codec.pth",
        607290935,
        "d15cbed16a40f478438c961fb043f68dfa6353bf56c966761315db3433e9722c",
    ),
    (
        "gpt.pth",
        3259599833,
        "43a8f4c30eccdf201958d3b9713511482c19d56dc20b0b1c4ee1e6b080b19d85",
    ),
    (
        "s2mel.pth",
        414908601,
        "9b1b0003fc189c94cc349758d7ebc25f903b7eb2de4602879959cc64ce816456",
    ),
    (
        "feat1.pt",
        57170,
        "f219cb447d80216ba615666da2ff8d63ac544eee26657f3a7b278692bf7a67c4",
    ),
    (
        "feat2.pt",
        374866,
        "9c4292e96dee535aea9a6206e9a0c856dd578dde9212acdb16dd3ada4d12bf80",
    ),
    (
        "multilingual_zh_ja_yue_char_del.tiktoken",
        907395,
        "747979631e813193436aabcff7c1c235d37de8097b71c563ec8b63b7a515c718",
    ),
    (
        "wav2vec2bert_stats.pt",
        9343,
        "c9c176c2b8850ab2e3ba828bbfa969deaf4566ce55db5f2687b8430b87526ad2",
    ),
)
PRIMARY_MODELS = tuple(
    ModelFile(name, MAIN_REPO, MAIN_REVISION, name, size, digest, MAIN_REPO)
    for name, size, digest in MAIN_FILES
)
AUXILIARY_MODELS = (
    ModelFile(
        "hf_cache/w2v-bert-2.0/config.json",
        "facebook/w2v-bert-2.0",
        "da985ba0987f70aaeb84a80f2851cfac8c697a7b",
        "config.json",
        1874,
        "f5572bd5998b68182e9c328a43127ed21fed687f6910497136b91a4e3b0e3675",
        "AI-ModelScope/w2v-bert-2.0",
    ),
    ModelFile(
        "hf_cache/w2v-bert-2.0/preprocessor_config.json",
        "facebook/w2v-bert-2.0",
        "da985ba0987f70aaeb84a80f2851cfac8c697a7b",
        "preprocessor_config.json",
        275,
        "8e6281aad64f97e40534135a59dcc5d33571efae376f2a25adf5551951897ab4",
        "AI-ModelScope/w2v-bert-2.0",
    ),
    ModelFile(
        "hf_cache/w2v-bert-2.0/model.safetensors",
        "facebook/w2v-bert-2.0",
        "da985ba0987f70aaeb84a80f2851cfac8c697a7b",
        "model.safetensors",
        2322063736,
        "eb890c9660ed6e3414b6812e27257b8ce5454365d5490d3ad581ea60b93be043",
        "AI-ModelScope/w2v-bert-2.0",
    ),
    ModelFile(
        "hf_cache/campplus_cn_common.bin",
        "funasr/campplus",
        "e4b6ede7ce16997aff4ae69fbca1f0175e2afede",
        "campplus_cn_common.bin",
        28036335,
        "3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8",
        "iic/speech_campplus_sv_zh-cn_16k-common",
    ),
    ModelFile(
        "hf_cache/bigvgan/config.json",
        "nvidia/bigvgan_v2_22khz_80band_256x",
        "633ff708ed5b74903e86ff1298cf4a98e921c513",
        "config.json",
        1405,
        "88a1f47acf747db0b21e97a389d838566147f7a5464583ff5c8d819d870f03ee",
    ),
    ModelFile(
        "hf_cache/bigvgan/bigvgan_generator.pt",
        "nvidia/bigvgan_v2_22khz_80band_256x",
        "633ff708ed5b74903e86ff1298cf4a98e921c513",
        "bigvgan_generator.pt",
        449228171,
        "e95ba25972d3de0628d99cd156e9315a9c018899bf739988959ebe3544080ced",
    ),
)
ALL_MODELS = PRIMARY_MODELS + AUXILIARY_MODELS
MODEL_BYTES = sum(item.size for item in ALL_MODELS)
MANIFEST_VERSION = "indextts-2.5-complete-v1"
