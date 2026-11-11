# CareNote AI

A lightweight clinical documentation app with a FastAPI backend and responsive vanilla JavaScript frontend. It organizes clinician-provided notes only; it does not diagnose or recommend treatment.

## Run locally

1. Install Python 3.11+.
2. In this folder, run `python -m venv .venv` and activate it.
3. Install dependencies: `pip install -r backend/requirements.txt`.
4. Copy `.env.example` to `.env` and set a long `SECRET_KEY`.
5. Start the API and static website: `python -m uvicorn backend.app.main:app --reload`.
6. Open http://127.0.0.1:8000. API docs are at `/docs`.

SQLite is used by default for a zero-setup local demonstration. Set `DATABASE_URL` to a PostgreSQL SQLAlchemy URL for PostgreSQL. `docker compose up --build` starts the app with PostgreSQL. Database tables are created on startup; production deployments should use a migration tool and a managed database. AI generation requires an `OPENAI_API_KEY`; without it, the API labels its conservative extraction mode as demonstration mode. Never use demo storage for real patient data.

## Scope and safety

Authentication uses hashed passwords and JWT bearer tokens. This starter is suitable for local demonstration, not production clinical use: it needs security review, HTTPS, operational controls, audited migrations, and a compliant hosting environment before handling real patient data. PDF export is browser print-to-PDF. No diagnosis or treatment recommendations are generated.
