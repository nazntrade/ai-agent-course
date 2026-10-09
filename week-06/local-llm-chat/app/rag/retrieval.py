"""Bounded multi-topic retrieval for questions naming several agent modules.

This improves evidence coverage, not the answer grader. All evidence comes from
the unchanged read-only index; no hand-written answers are inserted.
"""
import re
from .external_index import ExternalIndex

def retrieve(rag,embedder,question,*,top_k=5,min_score=None):
    if isinstance(rag,ExternalIndex):rag.verify_runtime_embedding(embedder)
    main=rag.search(embedder.embed_query(question),top_k=top_k,min_score=min_score)
    topics=re.findall(r'\b(profile|memory|planning|action)\b',question.lower())
    topics=list(dict.fromkeys(topics))
    if len(topics)<3 or not re.search(r'\b(roles?|modules?|architecture)\b',question.lower()):return main
    # A broad vector query tends to retrieve the framework figure repeatedly,
    # omitting prose definitions. Retrieve one definition per requested topic.
    result=[];seen=set()
    for topic in topics:
        found=rag.search(embedder.embed_query(f'{topic} module role definition in autonomous agent architecture'),top_k=2,min_score=min_score)
        for item in found:
            key=item.get('chunk_id') or item.get('text')
            if key not in seen:
                result.append(item);seen.add(key);break
    for item in main:
        key=item.get('chunk_id') or item.get('text')
        if key not in seen:result.append(item);seen.add(key)
    return result[:top_k]
