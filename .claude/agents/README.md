# Project Agents

Custom Claude-style agents for PaddleWebPipeline.

## Available Agents

- `paddle-backend-agent.md`
  - FastAPI, Celery, upload contracts, collection flow, worker metadata, backend fixes

- `workflow-ui-agent.md`
  - Next.js dashboard flow, collection UX, editor UX, visual consistency

- `rag-markdown-agent.md`
  - Paddle structured output, YAML frontmatter, markdown conversion, RAG output quality

- `runtime-ops-agent.md`
  - Docker, Celery runtime, queue stability, worker memory behavior, sequential processing

- `qa-agent.md`
  - Regression checks, test additions, build validation, contract verification

## Notes

These are written in the same broad style as the `.claude/agents` examples you referenced, but tailored to this repository's actual architecture:
- FastAPI backend
- Next.js frontend
- PaddleOCR + onnxruntime
- Celery + Redis workers
- RAG-oriented markdown generation
