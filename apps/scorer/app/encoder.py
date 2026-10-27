"""Offline CPU SentenceTransformer; no runtime network or fallback encoder."""
from .protocol import parse_json, validate_vectors


class Encoder:
    def __init__(self, manifest, threads=2):
        if type(threads) is not int or not 1 <= threads <= 4:
            raise ValueError('THREAD_LIMIT_INVALID')
        data = manifest.data
        tokenizer = data.get('tokenizer')
        if not isinstance(tokenizer, dict) or tokenizer.get('revision') != data['revision']:
            raise ValueError('TOKENIZER_NOT_FROZEN')
        if data.get('preprocessing') != 'identity': raise ValueError('PREPROCESSING_NOT_FROZEN')
        warmup = data.get('warmup_texts')
        if not isinstance(warmup, list) or not 1 <= len(warmup) <= 32 or any(not isinstance(t, str) or not 1 <= len(t) <= 256 for t in warmup):
            raise ValueError('WARMUP_NOT_FROZEN')
        # Explicitly freeze the modules this release knows; no custom module code.
        modules = parse_json((manifest.root / 'modules.json').read_bytes())
        allowed = {'sentence_transformers.models.Transformer', 'sentence_transformers.models.Pooling', 'sentence_transformers.models.Normalize'}
        if not isinstance(modules, list) or not modules or any(not isinstance(module, dict) or module.get('type') not in allowed for module in modules): raise ValueError('MODEL_MODULE_UNSUPPORTED')
        for module in modules:
            name = module.get('path')
            if not isinstance(name, str) or (name and ('\\' in name or ':' in name or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/')))):
                raise ValueError('MODEL_MODULE_PATH_UNSAFE')
        pooling_modules = [m for m in modules if m['type'] == 'sentence_transformers.models.Pooling']
        if len(pooling_modules) != 1: raise ValueError('POOLING_INVALID')
        pooling = parse_json((manifest.root / pooling_modules[0]['path'] / 'config.json').read_bytes())
        for mode in ('cls_token', 'mean_tokens', 'max_tokens', 'mean_sqrt_len_tokens', 'weightedmean_tokens', 'lasttoken'):
            expected = mode == {'cls': 'cls_token', 'mean': 'mean_tokens'}[data['pooling']]
            if bool(pooling.get('pooling_mode_' + mode, False)) != expected: raise ValueError('POOLING_MISMATCH')
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(threads)
        self.manifest = manifest
        self.model = SentenceTransformer(str(manifest.root), device='cpu', local_files_only=True,
                                         trust_remote_code=False, revision=data['revision'],
                                         tokenizer_kwargs={'local_files_only': True, 'trust_remote_code': False},
                                         model_kwargs={'local_files_only': True, 'trust_remote_code': False, 'torch_dtype': torch.float32, 'use_safetensors': True})
        if self.model.get_embedding_dimension() != manifest.dimension: raise ValueError('MODEL_DIMENSION_MISMATCH')
        self.encode(warmup)

    def encode(self, texts):
        vectors = self.model.encode(texts, batch_size=32, show_progress_bar=False,
                                    convert_to_numpy=True, normalize_embeddings=self.manifest.normalize,
                                    precision='float32', device='cpu').tolist()
        return validate_vectors(vectors, len(texts), self.manifest.dimension, self.manifest.normalize)
