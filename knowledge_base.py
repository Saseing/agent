"""
Enhanced Knowledge Base with Full RAG Pipeline
- Phase 1: Data Preparation & Indexing (smart chunking + parent-child)
- Phase 2: Query Understanding (rewriting, multi-query, HyDE)
- Phase 3: Multi-path Retrieval (vector + BM25 + RAG-Fusion + reranking)
- Phase 4: Context Injection & Compression
- Provides search_doc tool for Agent
"""

import os
import re
import math
import hashlib
import logging
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

from utils import extract_text as _shared_extract_text
from utils import (
    hf_model_is_cached,
    read_cached_model_file,
    sentence_transformers_importable as _sentence_transformers_safe,
)

logger = logging.getLogger("NewAgent.knowledge_base")

CHROMA_DIR = Path(__file__).parent / "knowledge_base" / "chroma"
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

# ── HuggingFace 镜像 ──────────────────────────────────────────────────────────
# huggingface.co 在国内网络下通常不可达（实测超时），hf-mirror.com 可用。
# 必须在任何 transformers / huggingface_hub 调用之前设置。
# 如需覆盖，在 .env 里设 HF_ENDPOINT 即可。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# 本地离线嵌入模型（无需联网，见 LocalHashingEmbedding）
LOCAL_EMBED_DIM = 512

# 中文语义嵌入模型。只要本地缓存中存在就会启用（见 hf_model_is_cached）；
# 未缓存时不联网下载，而是退化到 LocalHashingEmbedding ——
# 直接加载会让 SentenceTransformer 反复重试，实测阻塞 8 分钟以上。
ST_MODEL_NAME = "shibing624/text2vec-base-chinese"

try:
    import chromadb
    from chromadb.config import Settings
    HAS_CHROMA = True
except ImportError:
    HAS_CHROMA = False

# 注意：**不要**在这里 `from sentence_transformers import SentenceTransformer`。
# 本机环境下该 import 会触发原生层访问违例（进程以 0xC0000005 直接退出），
# try/except ImportError 完全拦不住 —— 一旦写在这里，整个 knowledge_base
# 模块都无法导入，服务直接起不来。
# 改为延迟到实际使用时经 utils.sentence_transformers_importable() 子进程探测后按需导入。
HAS_ST = True  # 仅表示"依赖已安装"，可用性以子进程探测结果为准

# ── 中文停用词 ──
_ZH_STOPWORDS = set(
    "的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 "
    "这 那 但 而 与 或 等 被 将 从 向 对 为 以 及 之 于 因 因 所 使 让 于 把 给 "
    "什么 怎么 如何 哪些 哪个 为什 这样 那样 这里 那里 现在 之前 之后 吗 呢 啊 吧 嘛".split()
)
_EN_STOPWORDS = set(
    "a an the and or but in on at to for of with by from as is are was were be been being "
    "have has had do does did will would shall should may might can could i you he she it "
    "we they me him her us them my your his its our their this that these those there here "
    "what which who when where why how not no so if than then very s t re ll ve d m".split()
)


# ═══════════════════════════════════════════════════════════════
# 嵌入函数
# ═══════════════════════════════════════════════════════════════

class LocalHashingEmbedding:
    """
    纯本地、零依赖（仅需 numpy）的离线嵌入函数。

    为什么需要它：
      原实现固定加载 SentenceTransformer('shibing624/text2vec-base-chinese')。
      该模型未随项目分发，在无外网环境下 SentenceTransformer 会反复重试
      huggingface.co 直到超时（实测 _sync_kb_index 阻塞 527 秒后失败），
      结果是 ChromaDB 永远是空的 —— Agent 的知识库检索工具完全不可用。

    做法：字符 n-gram（1/2/3）特征哈希 + 次线性 TF 加权 + L2 归一化。
    这是标准的哈希向量化技巧，对中文无需分词，且完全确定性、可离线运行。
    语义泛化能力弱于神经嵌入，但配合 BM25 与 RRF 融合已足够可用。
    """

    def __init__(self, dim: int = LOCAL_EMBED_DIM):
        self.dim = dim

    def _grams(self, text: str):
        text = (text or "").lower()
        for ch in text:
            if '\u4e00' <= ch <= '\u9fff' or ch.isalnum():
                yield ch
        # 中文以 bigram 为主，英文靠 1-gram 已可区分
        cjk = [c for c in text if '\u4e00' <= c <= '\u9fff']
        for i in range(len(cjk) - 1):
            yield cjk[i] + cjk[i + 1]
        for i in range(len(cjk) - 2):
            yield cjk[i] + cjk[i + 1] + cjk[i + 2]
        words = re.findall(r'[a-z0-9]{2,}', text)
        for w in words:
            yield w

    def encode_one(self, text: str):
        import numpy as np
        vec = np.zeros(self.dim, dtype=np.float32)
        counts = {}
        for gram in self._grams(text):
            counts[gram] = counts.get(gram, 0) + 1
        if not counts:
            return vec
        for gram, tf in counts.items():
            h = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:8], 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 31) & 1 else -1.0
            vec[idx] += sign * (1.0 + math.log(tf))
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    def encode_many(self, texts):
        import numpy as np
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self.encode_one(t) for t in texts])

    def __call__(self, input):  # ChromaDB EmbeddingFunction 协议
        import numpy as np
        if isinstance(input, str):
            input = [input]
        return self.encode_many(list(input)).tolist()

    # ChromaDB 1.5.x 分别用这两个方法处理文档与查询
    def embed_documents(self, input):
        import numpy as np
        if isinstance(input, str):
            input = [input]
        return self.encode_many(list(input)).tolist()

    def embed_query(self, input):
        import numpy as np
        if isinstance(input, str):
            input = [input]
        return self.encode_many(list(input)).tolist()

    # ChromaDB 会调用 name() 来校验集合上绑定的嵌入函数是否一致
    def name(self) -> str:
        return f"local-hashing-{self.dim}"

    # 新版 ChromaDB 的 EmbeddingFunction 协议要求
    @staticmethod
    def build_from_config(config):
        return LocalHashingEmbedding(dim=config.get("dim", LOCAL_EMBED_DIM))

    def get_config(self) -> dict:
        return {"dim": self.dim}

    def is_legacy(self) -> bool:
        return False

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list:
        return ["cosine", "l2", "ip"]


_local_embedder = None


def get_local_embedder() -> LocalHashingEmbedding:
    global _local_embedder
    if _local_embedder is None:
        _local_embedder = LocalHashingEmbedding()
    return _local_embedder


class TransformersEmbedding:
    """
    直接用 transformers + torch 加载句向量模型（均值池化）。

    为什么不直接用 SentenceTransformer：
      在本机环境下 `import sentence_transformers` 会触发原生层访问违例
      （进程直接以 0xC0000005 退出，没有任何 Python 异常可捕获）。
      sentence_transformers/__init__.py 会连带导入 trainer / onnx 导出器等
      一大堆模块，其中某个原生扩展与当前 torch/transformers 组合不兼容。
      崩溃发生在 import 阶段，try/except 拦不住，只能绕开。

    这里用 transformers 直接加载**同一份权重**，并复现该模型的池化方式
    （1_Pooling/config.json 指定 mean pooling + attention mask），
    效果等价且依赖面小得多。

    对外接口与 ChromaDB EmbeddingFunction 兼容。
    """

    MODEL_CONFIG_REL = "1_Pooling/config.json"

    def __init__(self, model_name: str = ST_MODEL_NAME):
        import json

        import torch
        from transformers import AutoModel, AutoTokenizer

        # 全程只在本地缓存里读，不发任何网络请求
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        self.model_name = model_name
        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, local_files_only=True)
        self.model = AutoModel.from_pretrained(model_name, local_files_only=True)
        self.model.eval()
        self.dim = int(self.model.config.hidden_size)

        # 读取模型的池化配置（从缓存直读，不走 hf_hub_download —— 它会发网请求）
        self.pooling_mode = "mean"
        self.include_prompt = True
        cfg_path = read_cached_model_file(model_name, self.MODEL_CONFIG_REL)
        if cfg_path:
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                if cfg.get("pooling_mode_cls_token"):
                    self.pooling_mode = "cls"
                elif cfg.get("pooling_mode_max_tokens"):
                    self.pooling_mode = "max"
                elif cfg.get("pooling_mode_mean_tokens"):
                    self.pooling_mode = "mean"
            except Exception as e:
                logger.debug("解析池化配置失败，使用 mean pooling: %s", e)
        else:
            logger.debug("未找到 %s，按 mean pooling 处理", self.MODEL_CONFIG_REL)

        # 模型自带的 query prompt 前缀（本模型没有）
        self.prompt = ""
        sc_path = read_cached_model_file(model_name, "config_sentence_transformers.json")
        if sc_path:
            try:
                self.prompt = (
                    json.loads(sc_path.read_text(encoding="utf-8"))
                    .get("prompts", {})
                    .get("query", "")
                    or ""
                )
            except Exception:
                pass

        logger.info(
            "语义嵌入已就绪: %s (维度=%d, 池化=%s, prompt=%r)",
            model_name, self.dim, self.pooling_mode, self.prompt,
        )

    def _pool(self, last_hidden, attention_mask):
        torch = self._torch
        if self.pooling_mode == "cls":
            return last_hidden[:, 0]
        if self.pooling_mode == "max":
            mask = attention_mask.unsqueeze(-1).expand(last_hidden.size()).float()
            return torch.max(last_hidden.masked_fill(mask == 0, -1e9), dim=1)[0]
        mask = attention_mask.unsqueeze(-1).expand(last_hidden.size()).float()
        summed = torch.sum(last_hidden * mask, dim=1)
        counts = torch.clamp(mask.sum(dim=1), min=1e-9)
        return summed / counts

    def encode_many(self, texts, batch_size: int = 32, is_query: bool = False):
        import numpy as np

        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if isinstance(texts, str):
            texts = [texts]

        prefix = self.prompt if (is_query and self.prompt and self.include_prompt) else ""
        payload = [prefix + t for t in texts] if prefix else list(texts)

        torch = self._torch
        out = []
        with torch.no_grad():
            for i in range(0, len(payload), batch_size):
                batch = payload[i:i + batch_size]
                enc = self.tokenizer(
                    batch, padding=True, truncation=True, max_length=512, return_tensors="pt"
                )
                last_hidden = self.model(**enc).last_hidden_state
                pooled = self._pool(last_hidden, enc["attention_mask"])
                pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
                out.append(pooled.cpu().numpy().astype(np.float32))
        return np.vstack(out) if out else np.zeros((0, self.dim), dtype=np.float32)

    # ── ChromaDB EmbeddingFunction 协议 ──────────────────────────────────────
    def __call__(self, input):
        if isinstance(input, str):
            input = [input]
        return self.encode_many(list(input)).tolist()

    def embed_documents(self, input):
        if isinstance(input, str):
            input = [input]
        return self.encode_many(list(input), is_query=False).tolist()

    def embed_query(self, input):
        if isinstance(input, str):
            input = [input]
        return self.encode_many(list(input), is_query=True).tolist()

    def name(self) -> str:
        return f"transformers-meanpool-{self.model_name.split('/')[-1]}"

    @staticmethod
    def build_from_config(config):
        return TransformersEmbedding(config.get("model_name", ST_MODEL_NAME))

    def get_config(self) -> dict:
        return {"model_name": self.model_name}

    def is_legacy(self) -> bool:
        return False

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list:
        return ["cosine", "l2", "ip"]


# ═══════════════════════════════════════════════════════════════
# Phase 1: Data Preparation & Indexing
# ═══════════════════════════════════════════════════════════════

def _tokenize(text: str) -> list:
    """中英混合文本分词：中文用bigram，英文用word，去停用词。"""
    if not text:
        return []
    text = text.lower().strip()
    tokens = []
    buf = ""
    i = 0
    while i < len(text):
        c = text[i]
        if '\u4e00' <= c <= '\u9fff':
            if buf:
                tokens.extend(_split_english(buf))
                buf = ""
            if i + 1 < len(text) and '\u4e00' <= text[i + 1] <= '\u9fff':
                tokens.append(text[i:i + 2])
            else:
                tokens.append(c)
            i += 1
        elif c.isalnum():
            buf += c
            i += 1
        else:
            if buf:
                tokens.extend(_split_english(buf))
                buf = ""
            i += 1
    if buf:
        tokens.extend(_split_english(buf))
    return [t for t in tokens if t and t not in _ZH_STOPWORDS and t not in _EN_STOPWORDS and len(t) > 1]


def _split_english(text: str) -> list:
    words = re.findall(r'[a-z]+', text.lower())
    return [w for w in words if w not in _EN_STOPWORDS and len(w) > 1]


def _clean_text(text: str) -> str:
    """清洗文本：剔除控制字符、重复页眉页脚，保留表格和数据。"""
    if not text:
        return ""
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', text)
    text = re.sub(r'_{3,}', '', text)
    lines = text.split('\n')
    seen_headers = {}
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        if re.match(r'^\d+$', stripped):
            continue
        if re.match(r'^第\s*\d+\s*页\s*$', stripped):
            continue
        if len(stripped) < 2 and not re.match(r'[a-zA-Z]', stripped):
            continue
        cleaned_lines.append(line)
    text = '\n'.join(cleaned_lines)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _split_sentences(text: str) -> list:
    """按中英文标点分割句子，保持语义完整性。"""
    if not text:
        return []
    parts = re.split(r'(?<=[。！？.!?；;])', text)
    result = []
    for p in parts:
        p = p.strip()
        if p:
            result.append(p)
    if not result and text.strip():
        result = [text.strip()]
    return result


def chunk_text_semantic(text: str, chunk_size: int = 500, overlap: int = 80) -> list:
    """
    智能语义分块（含页面溯源和质量过滤）：
    1. 解析 [第N页] 标记，按页分段
    2. 每页内按句子边界切分，保证语义完整
    3. 合并小句子，不超过 chunk_size
    4. 保留 overlap 重叠区，防止语义断裂
    5. 过滤掉过短的低质量块（如只有页码/标题）
    6. 每个块携带 page_num 元数据
    """
    if not text or not text.strip():
        return []
    cleaned = _clean_text(text)
    
    page_pattern = re.compile(r'\[第(\d+)页\]')
    page_sections = []
    last_end = 0
    
    for m in page_pattern.finditer(cleaned):
        if m.start() > last_end:
            pre_text = cleaned[last_end:m.start()].strip()
            if pre_text:
                page_sections.append((0, pre_text))
        page_num = int(m.group(1))
        section_start = m.end()
        next_m = page_pattern.search(cleaned, m.end())
        section_end = next_m.start() if next_m else len(cleaned)
        section_text = cleaned[section_start:section_end].strip()
        if section_text:
            page_sections.append((page_num, section_text))
        last_end = section_end
    
    if last_end < len(cleaned):
        remaining = cleaned[last_end:].strip()
        if remaining:
            page_sections.append((0, remaining))
    
    if not page_sections:
        page_sections = [(0, cleaned)]
    
    chunks = []
    for page_num, section_text in page_sections:
        sentences = _split_sentences(section_text)
        if not sentences:
            continue
        
        current = ""
        for s in sentences:
            if len(s) > chunk_size:
                if current:
                    chunks.append({"text": current, "page": page_num})
                    current = ""
                for i in range(0, len(s), chunk_size - overlap):
                    seg = s[i:i + chunk_size]
                    if seg and len(seg.strip()) >= 30:
                        chunks.append({"text": seg, "page": page_num})
                continue
            
            if len(current) + len(s) <= chunk_size:
                current = (current + s).strip()
            else:
                if current and len(current.strip()) >= 30:
                    chunks.append({"text": current, "page": page_num})
                if len(s) <= chunk_size:
                    current = s
                else:
                    for i in range(0, len(s), chunk_size - overlap):
                        seg = s[i:i + chunk_size]
                        if seg and len(seg.strip()) >= 30:
                            chunks.append({"text": seg, "page": page_num})
                    current = ""
        
        if current and len(current.strip()) >= 30:
            chunks.append({"text": current, "page": page_num})
    
    if not chunks:
        raw_chunks = [cleaned[i:i + chunk_size] for i in range(0, len(cleaned), chunk_size - overlap)]
        for rc in raw_chunks:
            if len(rc.strip()) >= 30:
                chunks.append({"text": rc, "page": 0})
    
    return chunks


def chunk_text_parent_child(text: str, chunk_size: int = 500, overlap: int = 80) -> dict:
    """
    父子分块（small-to-big）：
    - 父块 = 紧凑摘要（页号 + 首句 + 关键词），**用于建索引/检索**
      摘要去掉冗余表述、关键词密度高，比整段正文更好匹配问题
    - 子块 = 完整正文，**用于喂给模型生成**
      检索命中父块后，回带子块全文，保证上下文完整

    为什么这样做：直接用长正文做向量检索会"语义稀释"——一段 500 字里
    只有一两句跟你问的相关，整段向量被无关内容拉偏；用摘要检索准得多，
    而生成时又能拿到全文，两边都占。

    返回: {"parents": [...], "children": [...], "parent_child_map": {}}
    """
    chunks = chunk_text_semantic(text, chunk_size, overlap)
    if not chunks:
        return {"parents": [], "children": [], "parent_child_map": {}}

    parents = []
    children = []
    pc_map = {}

    for i, chunk_item in enumerate(chunks):
        chunk_body = chunk_item["text"] if isinstance(chunk_item, dict) else chunk_item
        page = chunk_item.get("page", 0) if isinstance(chunk_item, dict) else 0

        children.append({
            "id": f"child_{i}",
            "content": chunk_body,
            "char_length": len(chunk_body),
            "page": page,
        })

        # 摘要：取前两句（比只取首句更能表达块的主题），再压上关键词
        sentences = _split_sentences(chunk_body)
        head = " ".join(sentences[:2]) if sentences else chunk_body[:120]
        if len(head) > 200:
            head = head[:200]
        keywords = list(dict.fromkeys(_tokenize(chunk_body)))[:8]

        summary_parts = []
        if page > 0:
            summary_parts.append(f"[第{page}页]")
        summary_parts.append(head)
        if keywords:
            summary_parts.append("[关键词: " + ", ".join(keywords) + "]")
        parent_summary = " ".join(summary_parts)

        parents.append({
            "id": f"parent_{i}",
            "content": parent_summary,
            "child_id": f"child_{i}",
            "page": page,
            "char_length": len(parent_summary),
        })
        pc_map[f"parent_{i}"] = f"child_{i}"

    return {"parents": parents, "children": children, "parent_child_map": pc_map}


def extract_text(file_path: Path) -> str:
    """委托给 utils.extract_text，保持向后兼容。"""
    return _shared_extract_text(file_path)


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list:
    """向后兼容的分块接口，返回纯文本列表。"""
    chunks = chunk_text_semantic(text, chunk_size, max(overlap, 50))
    return [c["text"] if isinstance(c, dict) else c for c in chunks]


# ═══════════════════════════════════════════════════════════════
# BM25 实现（轻量级中文/英文混合 BM25）
# ═══════════════════════════════════════════════════════════════

class SimpleBM25:
    """轻量级 BM25 搜索引擎，支持中英文混合文本。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_count = 0
        self.avg_dl = 0.0
        self.doc_lens = []
        self.df = {}
        self.idf = {}
        self.doc_tokens = []
        self.doc_ids = []

    def build(self, documents: list, doc_ids: list = None):
        """从文档列表构建索引。"""
        self.doc_count = len(documents)
        self.doc_ids = doc_ids or [str(i) for i in range(self.doc_count)]
        self.doc_tokens = []
        self.doc_lens = []
        self.df = {}

        for doc in documents:
            tokens = _tokenize(doc)
            self.doc_tokens.append(tokens)
            self.doc_lens.append(len(tokens))
            seen = set()
            for t in set(tokens):
                self.df[t] = self.df.get(t, 0) + 1
                seen.add(t)

        if self.doc_count > 0:
            self.avg_dl = sum(self.doc_lens) / self.doc_count
        else:
            self.avg_dl = 0.0

        self.idf = {}
        for term, freq in self.df.items():
            self.idf[term] = math.log((self.doc_count - freq + 0.5) / (freq + 0.5) + 1)

    def search(self, query: str, top_k: int = 5) -> list:
        """搜索并返回 [(doc_id, score), ...] 列表。"""
        if self.doc_count == 0:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scores = {}
        for i, tokens in enumerate(self.doc_tokens):
            score = 0.0
            dl = self.doc_lens[i]
            tf_map = {}
            for t in tokens:
                tf_map[t] = tf_map.get(t, 0) + 1

            for qt in query_tokens:
                if qt not in self.idf:
                    continue
                tf = tf_map.get(qt, 0)
                if tf == 0:
                    continue
                idf = self.idf[qt]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / max(self.avg_dl, 1))
                score += idf * (tf * (self.k1 + 1)) / denom

            if score > 0:
                scores[self.doc_ids[i]] = score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    def get_scores(self, query: str) -> dict:
        """返回所有文档的 BM25 分数 {doc_id: score}。"""
        if self.doc_count == 0:
            return {}
        query_tokens = _tokenize(query)
        if not query_tokens:
            return {}

        scores = {}
        for i, tokens in enumerate(self.doc_tokens):
            score = 0.0
            dl = self.doc_lens[i]
            tf_map = {}
            for t in tokens:
                tf_map[t] = tf_map.get(t, 0) + 1

            for qt in query_tokens:
                if qt not in self.idf:
                    continue
                tf = tf_map.get(qt, 0)
                if tf == 0:
                    continue
                idf = self.idf[qt]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / max(self.avg_dl, 1))
                score += idf * (tf * (self.k1 + 1)) / denom

            if score > 0:
                scores[self.doc_ids[i]] = score

        return scores


# ═══════════════════════════════════════════════════════════════
# Phase 2-4: Retrieval Pipeline
# ═══════════════════════════════════════════════════════════════

def _get_llm():
    """懒加载 LLM，避免循环导入。"""
    try:
        from LCtext2 import llm as _llm
        return _llm
    except Exception:
        return None


_QUERY_REWRITE_PROMPT = (
    "你是一个搜索查询优化专家。请将用户的口语化问题改写为精准的书面搜索查询。\n"
    "规则：\n"
    "1. 保留核心实体和意图\n"
    "2. 去除口语化表达（如'那个'、'好像'、'啥'等）\n"
    "3. 使用专业术语\n"
    "4. 如果查询不完整，合理补全\n"
    "5. 只输出改写后的查询，不要解释\n\n"
    "用户问题：{query}\n"
    "改写后的查询："
)

_MULTI_QUERY_PROMPT = (
    "你是一个信息检索专家。针对以下用户问题，请从不同角度生成 {n} 个子查询，"
    "以确保检索的全面性。每个子查询应该关注问题的一个不同方面。\n\n"
    "格式：每行一个子查询，以数字编号。\n\n"
    "用户问题：{query}\n"
    "子查询："
)

_HYDE_PROMPT = (
    "你是一个文档问答专家。请针对用户问题，撰写一段假想的理想答案。\n"
    "这个答案将用于在向量数据库中找到最相关的真实文档，所以请尽量详细和具体。\n"
    "如果问题涉及具体数据、文档内容，请尝试引用具体的数字、名称等。\n"
    "直接输出假想答案，不要有开场白。\n\n"
    "用户问题：{query}\n"
    "假想答案："
)


class RetrievalPipeline:
    """
    完整 RAG 检索管道：
    1. 查询理解（rewrite / multi-query / HyDE）
    2. 向量 + BM25 多路召回
    3. RAG-Fusion 融合排序
    4. Embedding 重排序
    5. 上下文压缩
    """

    def __init__(self, kb_instance):
        self.kb = kb_instance
        self._bm25 = None
        self._bm25_docs = None
        self._bm25_doc_ids = []
        self._bm25_meta = []
        self._parent_index = None

    def _ensure_bm25(self):
        """
        确保 BM25 索引已构建（不可用时也保证 self._bm25 非 None）。

        BM25 索引的是**子块全文**，而不是 chroma 里的父块摘要。
        原因：BM25 是关键词召回，全文能覆盖更多词面命中；
        父块摘要做过截断和关键词压缩，会丢掉部分可在正文中出现的关键词。
        向量检索则相反 —— 用父亲摘要，语义更聚焦（small-to-big 的两条路各取所长）。
        """
        if self._bm25 is not None:
            return
        self._bm25 = SimpleBM25()
        try:
            if self.kb.collection.count() == 0:
                return
            all_data = self.kb.collection.get(include=["documents", "metadatas"])
            if not all_data.get("ids"):
                return
            docs = all_data["documents"]
            metas = all_data["metadatas"]
            # 优先用子块全文建 BM25 索引；缺 child_text 的老数据退回父块摘要
            bm25_docs = []
            for doc, meta in zip(docs, metas):
                bm25_docs.append((meta or {}).get("child_text") or doc)

            self._bm25_docs = bm25_docs
            self._bm25_doc_ids = all_data["ids"]
            self._bm25_meta = metas
            self._bm25.build(bm25_docs, self._bm25_doc_ids)
        except Exception as e:
            logger.warning("BM25 索引构建失败，本次退化为仅向量检索: %s", e)
            self._bm25 = SimpleBM25()

    def invalidate_bm25(self):
        """当知识库更新时调用。"""
        self._bm25 = None
        self._bm25_docs = None
        self._parent_index = None

    def _rewrite_query(self, query: str) -> str:
        """LLM 查询改写。"""
        llm = _get_llm()
        if llm is None:
            return query
        try:
            prompt = _QUERY_REWRITE_PROMPT.format(query=query)
            resp = llm.invoke(prompt)
            rewritten = resp.content.strip()
            return rewritten if rewritten else query
        except Exception as e:
            logger.debug("Query rewrite failed: %s", e)
            return query

    def _generate_multi_queries(self, query: str, n: int = 3) -> list:
        """LLM 多查询生成。"""
        llm = _get_llm()
        if llm is None:
            return [query]
        try:
            prompt = _MULTI_QUERY_PROMPT.format(query=query, n=n)
            resp = llm.invoke(prompt)
            lines = [l.strip().lstrip("0123456789.)、 ") for l in resp.content.strip().split("\n") if l.strip()]
            lines = [l for l in lines if l]
            if not lines:
                return [query]
            if query not in lines:
                lines.insert(0, query)
            return lines[:n + 1]
        except Exception as e:
            logger.debug("Multi-query failed: %s", e)
            return [query]

    def _hyde(self, query: str) -> str:
        """HyDE：生成假想文档作为查询。"""
        llm = _get_llm()
        if llm is None:
            return query
        try:
            prompt = _HYDE_PROMPT.format(query=query)
            resp = llm.invoke(prompt)
            hypothetical = resp.content.strip()
            return hypothetical if hypothetical else query
        except Exception as e:
            logger.debug("HyDE failed: %s", e)
            return query

    def _keyword_gate(self, query: str, items: list) -> list:
        """
        仅用离线哈希嵌入时，对检索结果做关键词重叠过滤。

        原因：哈希嵌入没有语义能力，实测「今天天气怎么样」这类无关查询的
        余弦距离反而比「公司考勤迟到怎么处罚」更近（0.808 vs 0.876），
        等于纯噪声。若不过滤，向量通道会把大量无关片段顶到前面，
        表现为「知识库检索返回答非所问的内容」。
        改用真实语义模型（本地已缓存时自动启用）后不需要此过滤。
        """
        if not items:
            return items
        if getattr(self.kb, "_embedding_backend", None) != "local-hashing":
            return items

        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return items

        kept = []
        for it in items:
            content_tokens = set(_tokenize(it.get("content", "")))
            overlap = query_tokens & content_tokens
            # 至少命中 40% 的查询词，且不是仅靠单个常见词命中
            if len(overlap) >= 2 and len(overlap) / len(query_tokens) >= 0.4:
                it = dict(it)
                it["keyword_overlap"] = len(overlap)
                kept.append(it)
        return kept

    def _vector_search(self, query: str, top_k: int) -> list:
        """
        向量相似度搜索（嵌入函数已在集合上绑定）。

        索引里存的是**父块摘要**，所以命中的是摘要；生成时用子块全文
        （metadata.child_text），实现 small-to-big。
        """
        if self.kb.collection.count() == 0:
            return []
        results = self.kb.collection.query(
            query_texts=[query],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        items = []
        if results["documents"] and results["documents"][0]:
            ids = results["ids"][0] if results.get("ids") else []
            for i, doc in enumerate(results["documents"][0]):
                distance = results["distances"][0][i] if results["distances"] else 0
                metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                record_id = ids[i] if i < len(ids) else f"vec_{i}"
                items.append({
                    "record_id": record_id,
                    "parent_text": metadata.get("parent_text", doc),
                    # 生成用全文
                    "content": metadata.get("child_text", doc),
                    "source": metadata.get("source", "未知"),
                    "chunk_index": metadata.get("chunk_index", 0),
                    "total_chunks": metadata.get("total_chunks", 0),
                    "page": metadata.get("page", 0),
                    "distance": distance,
                    "score": 1.0 / (1.0 + distance),
                })
        return items

    def _bm25_search(self, query: str, top_k: int) -> list:
        """
        BM25 关键词搜索。

        索引文本是 chroma 里的 document（即父块摘要，关键词密度高），
        返回时同样回带 child_text 全文，保证生成阶段上下文完整。
        """
        self._ensure_bm25()
        if self._bm25 is None or self._bm25.doc_count == 0:
            return []
        results = self._bm25.search(query, top_k=top_k * 2)
        items = []
        for rank, (doc_id, score) in enumerate(results):
            try:
                idx = self._bm25_doc_ids.index(doc_id)
            except ValueError:
                continue
            doc = self._bm25_docs[idx]
            meta = self._bm25_meta[idx] if self._bm25_meta else {}
            items.append({
                "record_id": doc_id,
                "parent_text": meta.get("parent_text", doc),
                "content": meta.get("child_text", doc),
                "source": meta.get("source", "未知"),
                "chunk_index": meta.get("chunk_index", 0),
                "total_chunks": meta.get("total_chunks", 0),
                "page": meta.get("page", 0),
                "distance": 0,
                "score": score,
                "bm25_score": score,
            })
        return items

    @staticmethod
    def _reciprocal_rank_fusion(rankings: list, k: int = 60) -> dict:
        """RAG-Fusion：倒数排名融合。rankings 是 list of (doc_id, score) 列表。"""
        rrf_scores = {}
        for ranking in rankings:
            for rank, (doc_id, _) in enumerate(ranking):
                rrf_scores[doc_id] = rrf_scores.get(doc_id, 0) + 1.0 / (k + rank + 1)
        return rrf_scores

    def _rerank(self, query: str, candidates: list, top_k: int) -> list:
        """
        重排序：用嵌入余弦相似度对候选结果精排。

        修复：原实现每次调用都新建 SentenceTransformer(...)，
        1) 无外网时必失败，2) 有外网时每个查询都重新加载整个模型。
        现在复用 KnowledgeBase 的嵌入后端（本地缓存模型或离线哈希嵌入）。
        """
        if not candidates:
            return []
        backend = getattr(self.kb, "_embedding_backend", "local-hashing")

        # 离线哈希嵌入没有语义能力，用它做余弦精排等于随机排序。
        # 这种情况改用 BM25 分数精排（BM25 在本项目里区分度良好）。
        if backend == "local-hashing":
            try:
                # 以「查询词重合度 + 原融合分」精排：稳定、便宜，且不依赖语义模型
                query_tokens = set(_tokenize(query))
                ranked = []
                for c in candidates[:top_k * 3]:
                    content_tokens = set(_tokenize(c["content"]))
                    overlap = len(query_tokens & content_tokens)
                    combined = (
                        0.7 * (overlap / max(len(query_tokens), 1))
                        + 0.3 * min(c.get("score", 0) * 10, 1.0)
                    )
                    item = dict(c)
                    item["rerank_score"] = combined
                    item["keyword_overlap"] = overlap
                    ranked.append((item, combined))
                ranked.sort(key=lambda x: x[1], reverse=True)
                return [it for it, _ in ranked[:top_k]]
            except Exception as e:
                logger.debug("关键词精排失败: %s", e)
                return candidates[:top_k]

        try:
            import numpy as np

            embed_fn = self.kb._get_embedding_fn()
            if embed_fn is None:
                return candidates[:top_k]

            # 精排必须用「父块摘要」编码 —— 向量库里存的就是父块摘要。
            # 若这里改用子块全文，等于拿另一种文本去比索引向量，
            # 相似度与召回阶段不可比，精排反而会引入噪声。
            query_emb = np.asarray(embed_fn([query])[0], dtype=np.float32)
            rerank_texts = [c.get("parent_text") or c["content"] for c in candidates]
            doc_embs = np.asarray(embed_fn(rerank_texts), dtype=np.float32)

            qn = float(np.linalg.norm(query_emb)) or 1.0
            scores = []
            for i, doc_emb in enumerate(doc_embs):
                dn = float(np.linalg.norm(doc_emb)) or 1.0
                sim = float(np.dot(query_emb, doc_emb) / (qn * dn + 1e-8))
                base = candidates[i].get("score", 0)
                combined = 0.7 * sim + 0.3 * min(base, 1.0)
                scores.append((i, combined))
                candidates[i]["_sim"] = sim

            scores.sort(key=lambda x: x[1], reverse=True)

            reranked = []
            for idx, score in scores[:top_k]:
                item = dict(candidates[idx])
                item["rerank_score"] = score
                item["semantic_similarity"] = candidates[idx].get("_sim", 0.0)
                reranked.append(item)
            return reranked

        except Exception as e:
            logger.debug("Rerank failed: %s", e)
            return candidates[:top_k]

    def _compress_context(self, results: list, query: str, max_tokens: int = 4000) -> list:
        """
        上下文压缩：对检索结果进行去重和相关性筛选，控制总 token 数。
        """
        if not results:
            return results

        seen_contents = set()
        deduped = []
        for r in results:
            content = r["content"]
            content_hash = hashlib.md5(content.encode()).hexdigest()
            if content_hash not in seen_contents:
                seen_contents.add(content_hash)
                deduped.append(r)

        scored = []
        query_tokens = set(_tokenize(query))
        for r in deduped:
            doc_tokens = set(_tokenize(r["content"]))
            overlap = len(query_tokens & doc_tokens)
            idf_score = overlap / max(len(query_tokens), 1)
            final_score = 0.7 * r.get("rerank_score", r.get("score", 0)) + 0.3 * idf_score
            scored.append((r, final_score))

        scored.sort(key=lambda x: x[1], reverse=True)

        selected = []
        total_chars = 0
        for r, _ in scored:
            content_len = len(r["content"])
            if total_chars + content_len > max_tokens * 2:
                break
            selected.append(r)
            total_chars += content_len

        return selected

    def retrieve(self, query: str, top_k: int = 5, mode: str = "balanced") -> list:
        """
        完整检索管道。
        mode:
          - "fast":        仅向量搜索，低延迟
          - "balanced":    向量 + BM25 + RAG-Fusion + 重排序（默认）
          - "quality":     查询改写 + 多查询 + HyDE + 完整管道
        """
        if self.kb.collection.count() == 0:
            return []

        if mode not in ("fast", "balanced", "quality"):
            mode = "balanced"

        if mode == "fast":
            return self._vector_search(query, top_k)

        # Phase 2: Query Understanding
        queries_to_run = [query]
        if mode == "quality":
            rewritten = self._rewrite_query(query)
            if rewritten != query:
                queries_to_run.append(rewritten)

            multi_queries = self._generate_multi_queries(query, n=3)
            queries_to_run.extend(multi_queries)

            hyde_query = self._hyde(query)
            if hyde_query != query:
                queries_to_run.append(hyde_query)

        # Phase 3: Multi-path Recall
        # 融合键统一用 chroma 记录 id：向量与 BM25 现在都返回 record_id，
        # 指向同一个「父块记录」。早前用下标/内容做键会导致映射错位，
        # 表现为「知识库有内容但检索恒为空」。
        all_vector_rankings = []
        all_bm25_rankings = []
        candidate_map = {}

        def _add_candidate(item: dict) -> str:
            key = item.get("record_id") or item["content"]
            if key not in candidate_map:
                candidate_map[key] = {
                    "doc_id": key,
                    # 生成用：子块全文
                    "content": item["content"],
                    # 检索用：父块摘要
                    "parent_text": item.get("parent_text", item["content"]),
                    "source": item.get("source", "未知"),
                    "chunk_index": item.get("chunk_index", 0),
                    "total_chunks": item.get("total_chunks", 0),
                    "page": item.get("page", 0),
                    "score": 0.0,
                    "distance": item.get("distance", 0),
                    "bm25_score": item.get("bm25_score", 0),
                }
            else:
                # 同一记录被多路命中时保留更好的 bm25 分
                if item.get("bm25_score"):
                    candidate_map[key]["bm25_score"] = max(
                        candidate_map[key].get("bm25_score", 0), item["bm25_score"]
                    )
            return key

        for q in queries_to_run:
            vector_results = self._keyword_gate(q, self._vector_search(q, top_k * 2))
            if vector_results:
                all_vector_rankings.append(
                    [(_add_candidate(r), r.get("score", 0)) for r in vector_results]
                )

            bm25_results = self._bm25_search(q, top_k * 2)
            if bm25_results:
                all_bm25_rankings.append(
                    [(_add_candidate(r), r.get("score", 0)) for r in bm25_results]
                )

        if not all_vector_rankings and not all_bm25_rankings:
            return []

        # RAG-Fusion：倒数排名融合，按 record_id 聚合
        all_rankings = all_vector_rankings + all_bm25_rankings
        fused_scores = self._reciprocal_rank_fusion(all_rankings)

        candidates = []
        for key, item in candidate_map.items():
            score = fused_scores.get(key, 0.0)
            if score <= 0:
                continue
            item = dict(item)
            item["score"] = score
            candidates.append(item)

        candidates.sort(key=lambda x: x["score"], reverse=True)

        if not candidates:
            return []

        # Phase 3b: Reranking
        reranked = self._rerank(query, candidates[:top_k * 3], top_k)

        # Phase 4: Context compression
        compressed = self._compress_context(reranked, query)

        return compressed


# ═══════════════════════════════════════════════════════════════
# KnowledgeBase 主类
# ═══════════════════════════════════════════════════════════════

class KnowledgeBase:
    def __init__(self):
        if not HAS_CHROMA:
            raise RuntimeError("chromadb 未安装，请执行: python -m pip install chromadb")
        self.client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        self._embedding_fn = None
        self._embedding_backend = None

        embed_fn = self._get_embedding_fn()
        cur_backend = self._embedding_backend
        cur_dim = getattr(embed_fn, "dim", None)

        recreate = False
        existing_meta = None

        try:
            _tmp = self.client.get_collection("documents")
            existing_meta = _tmp.metadata or {}
        except Exception:
            pass

        if existing_meta:
            old_backend = existing_meta.get("embedding_backend", "unknown")
            old_dim = existing_meta.get("embedding_dim", 0)
            backend_mismatch = old_backend != cur_backend
            dim_mismatch = int(old_dim or 0) != int(cur_dim or 0)
            if backend_mismatch or dim_mismatch:
                logger.warning(
                    "嵌入后端变更：%s(dim=%s) → %s(dim=%s)，将重建向量索引。"
                    "kb_store.json 中的文档会在启动索引自愈阶段自动重新入库。",
                    old_backend, old_dim, cur_backend, cur_dim,
                )
                try:
                    self.client.delete_collection("documents")
                except Exception:
                    pass
                recreate = True
        else:
            recreate = True

        self.collection = self.client.get_or_create_collection(
            name="documents",
            embedding_function=embed_fn,
            metadata={"embedding_backend": cur_backend, "embedding_dim": cur_dim},
        )

        self._needs_reindex = recreate
        self._pipeline = RetrievalPipeline(self)

    def _get_embedding_fn(self):
        """
        返回嵌入函数。优先使用本地已缓存的语义模型，否则退回离线哈希嵌入。

        关键：绝不在此处触发模型下载。原实现无条件构造
        SentenceTransformer(ST_MODEL_NAME)，模型未缓存时会联网重试，
        无外网时阻塞数分钟并使整个索引流程失败。
        """
        if self._embedding_fn is not None:
            return self._embedding_fn

        # 首选：transformers 直接加载（见 TransformersEmbedding 的说明）
        if hf_model_is_cached(ST_MODEL_NAME):
            try:
                self._embedding_fn = TransformersEmbedding(ST_MODEL_NAME)
                self._embedding_backend = "transformers"
                return self._embedding_fn
            except Exception as e:
                logger.warning("语义嵌入模型加载失败，尝试 sentence-transformers 兜底: %s", e)

            # 次选：sentence-transformers（本机 import 会原生崩溃，
            # 故必须先经子进程探测，探测不通过就绝不导入）
            if _sentence_transformers_safe():
                try:
                    from sentence_transformers import SentenceTransformer

                    class STEF(chromadb.utils.embedding_functions.EmbeddingFunction):
                        def __init__(self):
                            self.model = SentenceTransformer(ST_MODEL_NAME)

                        def __call__(self, input):
                            return self.model.encode(list(input)).tolist()

                        def name(self) -> str:
                            return "sentence-transformers-text2vec-base-chinese"

                    self._embedding_fn = STEF()
                    self._embedding_backend = "sentence-transformers"
                    logger.info("使用 sentence-transformers 嵌入: %s", ST_MODEL_NAME)
                    return self._embedding_fn
                except Exception as e:
                    logger.warning("sentence-transformers 加载失败: %s", e)

        self._embedding_fn = get_local_embedder()
        self._embedding_backend = "local-hashing"
        logger.warning(
            "未找到可用的语义嵌入模型，已退化为离线哈希嵌入（维度=%d）。"
            "哈希嵌入没有语义能力，检索质量会明显下降；"
            "下载 %s 到本地 HF 缓存即可自动启用真语义检索。",
            LOCAL_EMBED_DIM, ST_MODEL_NAME,
        )
        return self._embedding_fn

    def add_file(self, file_path: Path, original_filename: str) -> dict:
        text = extract_text(file_path)
        if text.startswith('[') and ('请安装' in text or '不支持' in text or '解析失败' in text or '无法提取' in text):
            return {"error": text}

        # 父子分块：索引用父块摘要，生成用子块全文（small-to-big）
        bundle = chunk_text_parent_child(text)
        parents = bundle["parents"]
        children = bundle["children"]
        if not parents:
            # 兜底：父子分块异常时退回普通语义分块
            chunks = chunk_text_semantic(text)
            if not chunks:
                return {"error": "文档为空或无法分块"}
            bundle = chunk_text_parent_child(text)
            parents, children = bundle["parents"], bundle["children"]
            if not parents:
                return {"error": "文档为空或无法分块"}

        child_by_id = {c["id"]: c for c in children}

        ids = []
        documents = []
        metadatas = []
        for i, parent in enumerate(parents):
            child = child_by_id.get(parent["child_id"], {})
            child_body = child.get("content", "") or parent["content"]

            # 记录 id 用「文件名+序号」的 md5，与父块一一对应
            chunk_id = hashlib.md5(f"{original_filename}_{i}".encode()).hexdigest()
            ids.append(chunk_id)
            # document 存父块摘要 —— 这是被向量化和 BM25 索引的文本
            documents.append(parent["content"])

            meta = {
                "source": original_filename,
                "chunk_index": i,
                "total_chunks": len(parents),
                "char_length": len(parent["content"]),
                # 子块全文随元数据一起存，检索命中后直接回带，无需二次查库
                "child_text": child_body,
                "parent_text": parent["content"],
                "child_id": parent["child_id"],
            }
            page = parent.get("page", 0)
            if page > 0:
                meta["page"] = page
            metadatas.append(meta)

        self._pipeline.invalidate_bm25()

        try:
            existing = self.collection.get(where={"source": original_filename})
            if existing["ids"]:
                self.collection.delete(ids=existing["ids"])
        except Exception:
            pass

        # 嵌入函数已在集合上绑定，这里不能再传 embedding_function
        self.collection.add(ids=ids, documents=documents, metadatas=metadatas)

        self._pipeline.invalidate_bm25()

        return {
            "filename": original_filename,
            "chunks": len(parents),
            "total_chars": len(text),
            "pages_found": len(set(p.get("page", 0) for p in parents if p.get("page", 0) > 0)),
            "chunking": "parent-child",
        }

    def search(self, query: str, top_k: int = 5, mode: str = "balanced") -> list:
        """
        增强的检索方法，支持三种模式：
        - "fast":        纯向量搜索
        - "balanced":    向量+BM25+RAG-Fusion+重排序（默认）
        - "quality":     查询改写+多查询+HyDE+完整管道
        """
        if self.collection.count() == 0:
            return []
        return self._pipeline.retrieve(query, top_k, mode)

    def search_simple(self, query: str, top_k: int = 5) -> list:
        """简单向量搜索（保持向后兼容）。"""
        if self.collection.count() == 0:
            return []
        results = self.collection.query(
            query_texts=[query],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        items = []
        if results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                distance = results["distances"][0][i] if results["distances"] else 0
                metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                items.append({
                    "content": doc,
                    "source": metadata.get("source", "未知"),
                    "chunk_index": metadata.get("chunk_index", 0),
                    "total_chunks": metadata.get("total_chunks", 0),
                    "page": metadata.get("page", 0),
                    "distance": distance,
                })
        return items

    def remove_file(self, filename: str) -> int:
        if self.collection.count() == 0:
            return 0
        all_data = self.collection.get(include=["metadatas"])
        ids_to_delete = []
        if all_data["ids"] and all_data["metadatas"]:
            for i, meta in enumerate(all_data["metadatas"]):
                if meta.get("source") == filename:
                    ids_to_delete.append(all_data["ids"][i])
        if ids_to_delete:
            self.collection.delete(ids=ids_to_delete)
            self._pipeline.invalidate_bm25()
        return len(ids_to_delete)

    def list_files(self) -> list:
        if self.collection.count() == 0:
            return []
        all_data = self.collection.get(include=["metadatas"])
        files = {}
        if all_data["metadatas"]:
            for meta in all_data["metadatas"]:
                src = meta.get("source", "未知")
                if src not in files:
                    files[src] = {"filename": src, "chunks": 0, "total_chunks": meta.get("total_chunks", 0)}
                files[src]["chunks"] += 1
        return list(files.values())


# ═══════════════════════════════════════════════════════════════
# 全局实例 & 工具函数
# ═══════════════════════════════════════════════════════════════

_kb_instance = None


def get_kb() -> Optional[KnowledgeBase]:
    global _kb_instance
    if _kb_instance is None and HAS_CHROMA:
        _kb_instance = KnowledgeBase()
    return _kb_instance


@tool
def search_doc(query: str, top_k: int = 5) -> str:
    """
    搜索知识库中的文档，返回与查询语义最相关的文档片段。
    当用户询问上传过的文档内容、要求查找资料或询问文件相关问题时，请使用此工具。
    当用户的问题与文档无关时，不要调用此工具，直接回答即可。
    query: 搜索查询，可以是自然语言问题
    top_k: 返回最相关的前k个片段，默认5
    """
    kb = get_kb()
    if kb is None:
        return "知识库未初始化。请先安装 chromadb：python -m pip install chromadb"
    files = kb.list_files()
    if not files:
        return "知识库中暂无文档。请先上传文档到知识库。"

    results = kb.search(query, top_k, mode="balanced")

    if not results:
        return f'知识库中未找到与"{query}"相关的内容。你可以尝试用更精确的关键词搜索，或使用 search_doc_quality 进行深度检索。'

    parts = [f"【知识库检索结果】共找到 {len(results)} 个相关片段：\n"]
    for i, item in enumerate(results, 1):
        source = item.get('source', '未知')
        page_info = f" 第{item['page']}页" if item.get('page') else ""
        parts.append(
            f"[片段 {i} | 来源: {source}{page_info}]\n"
            f"{item['content']}\n"
        )
    parts.append("请基于以上检索结果回答用户问题。如果检索结果不足，请告知用户，不要编造答案。")
    parts.append("如果需要更多上下文，请使用 read_file 工具读取原始文档的完整内容。")
    return "\n".join(parts)


@tool
def search_doc_fast(query: str, top_k: int = 5) -> str:
    """快速搜索知识库（仅向量检索，速度快但精度稍低）。
    适用于简单明确的查询。"""
    kb = get_kb()
    if kb is None:
        return "知识库未初始化。请先安装 chromadb：python -m pip install chromadb"
    files = kb.list_files()
    if not files:
        return "知识库中暂无文档。"
    results = kb.search(query, top_k, mode="fast")
    if not results:
        return f'知识库中未找到与"{query}"相关的内容。'
    parts = [f"【快速检索结果】共 {len(results)} 个片段：\n"]
    for i, item in enumerate(results, 1):
        parts.append(f"[片段 {i} | {item['source']}]\n{item['content']}\n")
    return "\n".join(parts)


@tool
def search_doc_quality(query: str, top_k: int = 5) -> str:
    """高质量知识库搜索（查询改写+多查询+HyDE+完整管道）。
    适用于复杂、模糊或需要高精度的查询。速度较慢但召回率更高。"""
    kb = get_kb()
    if kb is None:
        return "知识库未初始化。请先安装 chromadb：python -m pip install chromadb"
    files = kb.list_files()
    if not files:
        return "知识库中暂无文档。"
    results = kb.search(query, top_k, mode="quality")
    if not results:
        return f'知识库中未找到与"{query}"相关的内容。建议尝试不同的关键词。'
    parts = [f"【深度检索结果】共 {len(results)} 个相关片段：\n"]
    for i, item in enumerate(results, 1):
        parts.append(f"[片段 {i} | 来源: {item['source']}]\n{item['content']}\n")
    parts.append("请综合以上片段回答。如果信息不足，请告知用户。")
    return "\n".join(parts)