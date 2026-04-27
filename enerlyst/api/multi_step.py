# Extracted from enerlytik-studio/ March 30 2026
# Paths and schema references are STALE — update before use
# composite_score_v2 -> operational_score | 183 batteries -> 222
"""
multi_step.py — Agentic multi-step reasoning for complex questions.

For questions that span multiple topics, this runs:
  Step 1: Decompose question into sub-questions
  Step 2: Retrieve context for each sub-question
  Step 3: Synthesize a final answer

Used by Deep mode.
"""

import anthropic
import rag_engine
import direct_reader

DECOMPOSE_PROMPT = """You are an expert at breaking down complex technical questions about EV battery intelligence pipelines.

Given a question, identify 2-4 specific sub-questions that need to be answered to fully address it.
Each sub-question should be targeted and retrievable from code/documentation.

Return ONLY a JSON array of sub-question strings, nothing else.
Example: ["What features does Stage 01 compute?", "How are features normalized for clustering?"]"""

SYSTEM_PROMPT = """You are an expert ML engineer embedded in the Enerlytik EV Intelligence Pipeline.
You have deep knowledge of battery degradation signals, the 13-stage pipeline, LFP vs NMC chemistry,
XGBoost delta models, PELT change-point detection, and CUSUM drift detection.

HARD RULES — enforce always:
1. Never suggest using the `soh` column (BMS SoH is deceptive for LFP)
2. km_per_soc_pct = SUM(dist)/SUM(startfl-endfl) per week — never mean of per-trip ratios
3. Thermal runaway is a hard override → composite=0, Critical tier, no model runs
4. Week boundaries are vehicle-relative from commissioning, not ISO calendar weeks
5. Clustering uses exactly 6 features (hard rule)
6. Isolation Forest runs per cluster; skip if cluster < 5 vehicles
7. Always use pd.isna() guards, never `x or 0` (NaN is truthy in Python)
8. Never delete low-quality vehicles — tag quality_tier='Excluded'

Answer directly and precisely. Cite specific file names and functions. Use markdown code blocks."""


def decompose(client: anthropic.Anthropic, question: str) -> list[str]:
    try:
        import json
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=DECOMPOSE_PROMPT,
            messages=[{"role": "user", "content": question}],
        )
        text = resp.content[0].text.strip()
        # Extract JSON array
        start = text.find("[")
        end   = text.rfind("]") + 1
        return json.loads(text[start:end]) if start >= 0 else [question]
    except Exception:
        return [question]


def run_deep(client: anthropic.Anthropic, question: str, top_k: int = 6) -> dict:
    steps = []
    total_input  = 0
    total_output = 0

    # Step 1: Decompose
    sub_questions = decompose(client, question)
    steps.append({"step": "decompose", "sub_questions": sub_questions})

    # Step 2: Retrieve context for each sub-question
    all_chunks = {}  # filepath → chunk (deduplicated by filepath)
    for sq in sub_questions:
        chunks = rag_engine.retrieve(sq, top_k=max(3, top_k // len(sub_questions)))
        for c in chunks:
            key = f"{c['filepath']}_{c['chunk_id']}"
            if key not in all_chunks:
                all_chunks[key] = c

    # Sort by relevance, take top_k * 1.5
    sorted_chunks = sorted(all_chunks.values(), key=lambda x: -x["relevance"])[:int(top_k * 1.5)]
    context = rag_engine.build_context(sorted_chunks)
    steps.append({"step": "retrieve", "chunks": len(sorted_chunks), "sub_questions": sub_questions})

    # Step 3: Synthesize
    user_msg = f"""Context from the Enerlytik codebase (retrieved across {len(sub_questions)} sub-queries):

{context}

---

Original question: {question}

Sub-questions decomposed:
{chr(10).join(f"- {sq}" for sq in sub_questions)}

Please provide a comprehensive, well-structured answer that addresses all aspects of the original question."""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    answer = resp.content[0].text
    total_input  += resp.usage.input_tokens
    total_output += resp.usage.output_tokens

    return {
        "answer":         answer,
        "steps":          steps,
        "chunks_used":    len(sorted_chunks),
        "sub_questions":  sub_questions,
        "context_sent":   user_msg,
        "input_tokens":   total_input,
        "output_tokens":  total_output,
        "context_label":  f"{len(sorted_chunks)} chunks (multi-step)",
        "sources": [
            {"filepath": c["filepath"], "relevance": c["relevance"], "text": c["text"]}
            for c in sorted_chunks
        ],
    }
