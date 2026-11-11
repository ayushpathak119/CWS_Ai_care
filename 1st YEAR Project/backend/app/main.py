from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from openai import OpenAI
from pwdlib import PasswordHash
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import DateTime, ForeignKey, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
SECRET = os.getenv("SECRET_KEY", "local-demo-secret-change-before-deploy")
DB_URL = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'carenote.db'}")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
password_hash = PasswordHash.recommended()
oauth = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="Doctor")
    password: Mapped[str] = mapped_column(String(255))

class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    age: Mapped[int | None] = mapped_column(nullable=True)
    gender: Mapped[str] = mapped_column(String(40), default="Not specified")
    contact: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[list["ClinicalNote"]] = relationship(back_populates="patient", cascade="all, delete-orphan")

class ClinicalNote(Base):
    __tablename__ = "clinical_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="Draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    patient: Mapped[Patient] = relationship(back_populates="notes")

Base.metadata.create_all(engine)
app = FastAPI(title="CareNote AI", version="1.0.0")

def db_session():
    with SessionLocal() as db: yield db

def current_user(token: str = Depends(oauth), db: Session = Depends(db_session)) -> User:
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        user = db.get(User, int(payload["sub"]))
    except Exception as exc: raise HTTPException(401, "Invalid or expired session") from exc
    if not user: raise HTTPException(401, "Account not found")
    return user

class Register(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    role: str = "Doctor"

class PatientIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    age: int | None = Field(default=None, ge=0, le=130)
    gender: str = Field(default="Not specified", max_length=40)
    contact: str = Field(default="", max_length=40)

class NoteIn(BaseModel):
    patient_id: int
    raw_text: str = Field(min_length=1, max_length=12000)
    content: str = Field(min_length=1, max_length=20000)

def patient_out(p: Patient): return {"id": p.id, "name": p.name, "age": p.age, "gender": p.gender, "contact": p.contact}
def note_out(n: ClinicalNote): return {"id": n.id, "patient_id": n.patient_id, "patient": n.patient.name, "content": n.content, "raw_text": n.raw_text, "status": n.status, "created_at": n.created_at.isoformat()}

@app.get("/api/health")
def health(): return {"status": "ok", "ai_configured": bool(os.getenv("OPENAI_API_KEY"))}

@app.post("/api/auth/register")
def register(data: Register, db: Session = Depends(db_session)):
    if data.role not in {"Doctor", "Administrator"}: raise HTTPException(400, "Choose Doctor or Administrator")
    email = str(data.email).lower()
    if db.scalar(select(User).where(User.email == email)): raise HTTPException(409, "An account with this email already exists")
    user = User(email=email, name=data.name.strip(), role=data.role, password=password_hash.hash(data.password))
    db.add(user); db.commit(); db.refresh(user)
    return issue_token(user)

def issue_token(user: User):
    token = jwt.encode({"sub": str(user.id), "exp": datetime.now(timezone.utc) + timedelta(hours=8)}, SECRET, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "user": {"name": user.name, "email": user.email, "role": user.role}}

@app.post("/api/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(db_session)):
    user = db.scalar(select(User).where(User.email == form.username.lower()))
    if not user or not password_hash.verify(form.password, user.password): raise HTTPException(status_code=401, detail="Email or password is incorrect", headers={"WWW-Authenticate": "Bearer"})
    return issue_token(user)

@app.get("/api/auth/me")
def me(user: User = Depends(current_user)): return {"name": user.name, "email": user.email, "role": user.role}

@app.get("/api/patients")
def list_patients(q: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    query = select(Patient).where(Patient.owner_id == user.id)
    if q.strip(): query = query.where(Patient.name.ilike(f"%{q.strip()}%"))
    rows = db.scalars(query.order_by(Patient.created_at.desc())).all()
    return [{**patient_out(p), "consultations": len(p.notes)} for p in rows]

@app.post("/api/patients", status_code=201)
def add_patient(data: PatientIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    p = Patient(**data.model_dump(), owner_id=user.id); db.add(p); db.commit(); db.refresh(p); return patient_out(p)

@app.put("/api/patients/{patient_id}")
def update_patient(patient_id: int, data: PatientIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    p = db.scalar(select(Patient).where(Patient.id == patient_id, Patient.owner_id == user.id))
    if not p: raise HTTPException(404, "Patient not found")
    for k, v in data.model_dump().items(): setattr(p, k, v)
    db.commit(); db.refresh(p); return patient_out(p)

@app.delete("/api/patients/{patient_id}", status_code=204)
def delete_patient(patient_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    p = db.scalar(select(Patient).where(Patient.id == patient_id, Patient.owner_id == user.id))
    if not p: raise HTTPException(404, "Patient not found")
    db.delete(p); db.commit()

def demo_structure(raw: str):
    # Conservative local demo: copies only recognizable clinician-provided clauses.
    sections = {"Chief complaint": "Not specified", "Duration": "Not specified", "Symptoms": "Not specified", "Observations": "Not specified", "Tests advised": "Not specified", "Follow-up": "Not specified"}
    import re
    patterns = {"Duration": r"\b(?:for|since)\s+[^,.]+", "Observations": r"\b(?:BP|blood pressure|pulse|temperature|SpO2)\s+(?:is\s+)?[^,.]+", "Tests advised": r"\b(?:test|tests|blood test|x-ray|scan)\s+(?:advised|ordered|recommended)[^,.]*", "Follow-up": r"\bfollow[- ]?up\s+(?:after|in|on)[^,.]+"}
    for label, pattern in patterns.items():
        match = re.search(pattern, raw, re.I)
        if match: sections[label] = match.group(0).strip()
    first = re.split(r"[.!?\n]", raw.strip())[0]
    if first: sections["Chief complaint"] = first[:300]
    return sections

@app.post("/api/notes/generate")
def generate_note(payload: dict, user: User = Depends(current_user)):
    raw = str(payload.get("raw_text", "")).strip()
    if not raw or len(raw) > 12000: raise HTTPException(422, "Enter notes between 1 and 12,000 characters")
    key = os.getenv("OPENAI_API_KEY")
    if not key: return {"sections": demo_structure(raw), "mode": "demonstration", "message": "Demonstration extraction. Review every field; no AI provider is configured."}
    try:
        client = OpenAI(api_key=key, timeout=20, max_retries=1)
        response = client.chat.completions.create(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"), response_format={"type": "json_object"}, messages=[{"role":"system","content":"Organize only facts explicitly present in the clinician's notes into JSON keys: Chief complaint, Duration, Symptoms, Observations, Tests advised, Follow-up. Use Not specified for missing data. Preserve ambiguity. Never infer diagnoses, recommend treatment, or invent facts. Return JSON object only."},{"role":"user","content":raw}])
        import json
        sections = json.loads(response.choices[0].message.content or "{}")
        expected = ["Chief complaint", "Duration", "Symptoms", "Observations", "Tests advised", "Follow-up"]
        safe = {k: str(sections.get(k, "Not specified"))[:1000] for k in expected}
        return {"sections": safe, "mode": "ai", "message": "AI draft. Doctor review required."}
    except Exception as exc: raise HTTPException(502, "AI service failed. Please retry or use the clearly labeled demonstration mode.") from exc

@app.get("/api/notes")
def list_notes(q: str = "", status_filter: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    query = select(ClinicalNote).join(Patient).where(ClinicalNote.owner_id == user.id)
    if q: query = query.where(Patient.name.ilike(f"%{q}%") | ClinicalNote.content.ilike(f"%{q}%"))
    if status_filter in {"Draft", "Approved"}: query = query.where(ClinicalNote.status == status_filter)
    return [note_out(n) for n in db.scalars(query.order_by(ClinicalNote.created_at.desc()).limit(100)).all()]

@app.post("/api/notes", status_code=201)
def create_note(data: NoteIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    patient = db.scalar(select(Patient).where(Patient.id == data.patient_id, Patient.owner_id == user.id))
    if not patient: raise HTTPException(404, "Patient not found")
    note = ClinicalNote(**data.model_dump(), owner_id=user.id); db.add(note); db.commit(); db.refresh(note); return note_out(note)

@app.get("/api/notes/{note_id}")
def get_note(note_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    note = db.scalar(select(ClinicalNote).where(ClinicalNote.id == note_id, ClinicalNote.owner_id == user.id))
    if not note: raise HTTPException(404, "Note not found")
    return note_out(note)

@app.put("/api/notes/{note_id}")
def update_note(note_id: int, data: NoteIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    note = db.scalar(select(ClinicalNote).where(ClinicalNote.id == note_id, ClinicalNote.owner_id == user.id))
    patient = db.scalar(select(Patient).where(Patient.id == data.patient_id, Patient.owner_id == user.id))
    if not note or not patient: raise HTTPException(404, "Note or patient not found")
    if note.status == "Approved": raise HTTPException(409, "Approved notes cannot be edited")
    note.patient_id = data.patient_id; note.raw_text = data.raw_text; note.content = data.content
    db.commit(); db.refresh(note); return note_out(note)

@app.delete("/api/notes/{note_id}", status_code=204)
def delete_note(note_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    note = db.scalar(select(ClinicalNote).where(ClinicalNote.id == note_id, ClinicalNote.owner_id == user.id))
    if not note: raise HTTPException(404, "Note not found")
    if note.status == "Approved": raise HTTPException(409, "Approved notes cannot be deleted")
    db.delete(note); db.commit()

@app.post("/api/notes/{note_id}/approve")
def approve_note(note_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    note = db.scalar(select(ClinicalNote).where(ClinicalNote.id == note_id, ClinicalNote.owner_id == user.id))
    if not note: raise HTTPException(404, "Note not found")
    note.status = "Approved"; db.commit(); db.refresh(note); return note_out(note)

@app.get("/api/patients/{patient_id}/timeline")
def timeline(patient_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    patient = db.scalar(select(Patient).where(Patient.id == patient_id, Patient.owner_id == user.id))
    if not patient: raise HTTPException(404, "Patient not found")
    return [note_out(n) for n in db.scalars(select(ClinicalNote).where(ClinicalNote.patient_id == patient_id, ClinicalNote.owner_id == user.id).order_by(ClinicalNote.created_at.desc())).all()]

@app.get("/api/notes/{note_id}/pdf")
def note_pdf(note_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    note = db.scalar(select(ClinicalNote).where(ClinicalNote.id == note_id, ClinicalNote.owner_id == user.id))
    if not note: raise HTTPException(404, "Note not found")
    if note.status != "Approved": raise HTTPException(409, "Only approved notes can be exported")
    return {"print_view": True, **note_out(note)}

@app.get("/{path:path}")
def frontend(path: str):
    frontend_root = (ROOT / "frontend").resolve()
    file = (frontend_root / (path or "index.html")).resolve()
    if not file.is_file() or frontend_root not in file.parents: file = frontend_root / "index.html"
    return FileResponse(file)
