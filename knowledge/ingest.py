"""文档入库：读取文档，切分后写入向量库。

支持格式：.md / .txt / .pdf / .doc / .docx / .xlsx / .xls（见 extractors.py）。
流程：原始文档 -> 提取纯文本 -> 按段落切分（chunk）-> Embedding 向量化 -> 存入 Chroma。
重复执行会先删除同名文档的旧片段，因此可以安全地反复入库。

按用户隔离：ingest_file(path, user_id) 写入该用户专属的 Chroma collection。
"""
import config
from knowledge.extractors import SUPPORTED_EXTS, extract_text
from knowledge.retriever import get_kb


def chunk_text(text, chunk_size=config.CHUNK_SIZE, overlap=config.CHUNK_OVERLAP):
    """把长文本切成若干片段。

    策略：先按空行拆成自然段落，再把段落拼到接近 chunk_size 为止；
    超过 chunk_size 的长段落做硬切，并保留 overlap 字符的重叠。
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        if len(current) + len(p) + 2 <= chunk_size:
            current = f"{current}\n\n{p}" if current else p
        else:
            if current:
                chunks.append(current)
            while len(p) > chunk_size:  # 超长段落硬切
                chunks.append(p[:chunk_size])
                p = p[chunk_size - overlap:]
            current = p
    if current:
        chunks.append(current)
    return chunks


def ingest_file(path, user_id):
    """入库单个文件：提取 -> 切片 -> 删旧 -> 加新。返回切片数。

    写入 user_id 对应的 Chroma collection，同进程检索立即可见。
    """
    text = extract_text(path)
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError(f"文件 {path.name} 内容为空，跳过入库")
    kb = get_kb(user_id)
    kb.delete_by_source(path.name)
    kb.add(
        chunks,
        [{"source": path.name}] * len(chunks),
        [f"{path.stem}-{i}" for i in range(len(chunks))],
    )
    return len(chunks)


def delete_file(path, user_id):
    """从向量库删除某文件的全部片段。"""
    kb = get_kb(user_id)
    kb.delete_by_source(path.name)


def ingest_docs(user_id):
    """把该用户目录下所有受支持的文档入库，打印进度。CLI 用。"""
    docs_dir = config.DOCS_DIR / str(user_id)
    docs_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(
        p for p in docs_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )
    if not files:
        print(
            f"在 {docs_dir} 中没有找到可入库的文档"
            f"（支持：{'/'.join(SUPPORTED_EXTS)}），请先放入文件。"
        )
        return

    kb = get_kb(user_id)
    total = 0
    for f in files:
        try:
            n = ingest_file(f, user_id)
        except Exception as e:
            print(f"  跳过 {f.name}：{e}")
            continue
        total += n
        print(f"  {f.name} -> {n} 个片段")
    print(f"入库完成，共 {total} 个片段，知识库总片段数：{kb.count()}")
