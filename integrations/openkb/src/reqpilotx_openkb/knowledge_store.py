"""Folder/upload knowledge snapshots and original-reference resolver.

Exploration only. Reuses actual ReqPilotX register/verify_catalog/capture.
Single-host local filesystem; no model calls, wiki prose parsing or main edits.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from types import SimpleNamespace
import uuid
import zipfile

from .errors import AdapterError
from reqpilotx.domain.knowledge.evidence import compute_evidence_content_hash
from reqpilotx.domain.knowledge.lifecycle import evaluate_knowledge_evidence_eligibility
from reqpilotx.application.agent_workflow.source_requirement_baseline import canonical_json_sha256

from evidence_tools import register, verify_catalog  # Real existing registration chain.

ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
GEN = re.compile(r"^generation-[0-9a-f]{64}$")
FORMATS = {".txt", ".docx", ".xlsx", ".pdf"}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES = 64


def digest(value):
    return canonical_json_sha256(value)


def checked_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise AdapterError("Unsafe identity")
    return value


def logical_name(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise AdapterError("Unsafe file name")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise AdapterError("Unsafe relative file path")
    if path.suffix.lower() not in FORMATS:
        raise AdapterError("Prototype supports TXT, DOCX, XLSX and PDF only")
    if len(value) > 240:
        raise AdapterError("File name exceeds exploration limit")
    return path.as_posix()


def sealed(value):
    value = deepcopy(value)
    value["record_sha256"] = digest(value)
    return value


def unseal(value):
    if not isinstance(value, dict) or value.get("record_sha256") != digest(
        {k: v for k, v in value.items() if k != "record_sha256"}
    ):
        raise AdapterError("Stored record integrity changed")
    return value


def write_json(path, value, *, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise AdapterError("Symbolic-link storage target is not allowed")
    temp = path.parent / (".atomic-" + uuid.uuid4().hex)
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(0o600)
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)  # Never overwrite an immutable record.
            temp.unlink()
    finally:
        temp.unlink(missing_ok=True)


class FolderKnowledgeStore:
    """Host-owned folder scope with immutable, content-addressed revisions.

    record hashes detect accidental corruption, not a malicious store owner.
    Use OS isolation; this prototype does not implement SSO or remote ACLs.
    """
    def __init__(self, root, project_id, kb, *, as_of=None):
        self.project_id, self.kb = checked_id(project_id), checked_id(kb)
        self._as_of = as_of
        original = Path(root).absolute()
        if original.is_symlink():
            raise AdapterError("Storage root cannot be a symbolic link")
        original.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = original.resolve()
        self.state_path = self.root / "state.json"
        with self.lock():
            if not self.state_path.exists():
                write_json(self.state_path, sealed({"schema_version": "openkb-folder-store/v1",
                    "project_id": self.project_id, "kb": self.kb,
                    "current_generation": None, "revision": 0, "documents": {}}))
            self._state()

    def inside(self, relative):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise AdapterError("Storage path escapes root")
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise AdapterError("Symbolic links inside storage are not allowed")
        return path

    @contextmanager
    def lock(self):
        lock = self.inside(".store.lock")
        with lock.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _state(self):
        value = unseal(json.loads(self.inside("state.json").read_text()))
        if value["project_id"] != self.project_id or value["kb"] != self.kb:
            raise AdapterError("Store belongs to another project/KB")
        return value

    @property
    def current_generation(self):
        return self._state()["current_generation"]

    @property
    def as_of(self):
        return self._as_of or date.today()

    def _validate_bytes(self, filename, content):
        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_FILE_BYTES:
            raise AdapterError("Empty, invalid or over-budget uploaded bytes")
        if Path(filename).suffix.lower() in {".docx", ".xlsx"}:
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    entries = archive.infolist()
                    if (len(entries) > 10000 or sum(x.file_size for x in entries) > 30 * 1024 * 1024
                            or any(x.flag_bits & 1 for x in entries)):
                        raise AdapterError("Encrypted or over-budget Office container")
            except zipfile.BadZipFile as exc:
                raise AdapterError("Invalid Office container") from exc

    def upload_files(self, files, *, metadata=None, declaration="Synthetic host upload", replace_all=False):
        """files: logical relative name -> bytes; no arbitrary destination paths.

        Metadata is user/host declared, never extracted from model prose.
        replace_all=True is an explicit folder mirror; absent files become inactive.
        """
        if not isinstance(files, dict) or len(files) > MAX_FILES:
            raise AdapterError("Invalid or over-budget file collection")
        if not isinstance(declaration, str) or not declaration.strip():
            raise AdapterError("Explicit host/user registration declaration required")
        metadata = metadata or {}
        if not isinstance(metadata, dict):
            raise AdapterError("Invalid registration metadata")
        prepared = []
        for name, content in sorted(files.items()):
            name = logical_name(name)
            self._validate_bytes(name, content)
            supplied = deepcopy(metadata.get(name, {}))
            if not isinstance(supplied, dict):
                raise AdapterError("Invalid document metadata")
            forbidden = {"id", "project_id", "file_name", "source_sha256", "status", "doc_version"}
            if forbidden.intersection(supplied):
                raise AdapterError("Host-generated identity cannot be overridden")
            if set(supplied) - {"doc_type", "authority", "claim_role", "applicability", "effective_from", "effective_to"}:
                raise AdapterError("Unknown registration metadata field")
            if any((value is not None or key not in {"effective_from", "effective_to"})
                   and (not isinstance(value, str) or len(value) > 4000)
                   for key, value in supplied.items()):
                raise AdapterError("Registration metadata must use bounded strings or optional null dates")
            prepared.append((name, content, supplied))
        with self.lock():
            state = self._state()
            documents = {} if replace_all else deepcopy(state["documents"])
            for name, content, supplied in prepared:
                sha = hashlib.sha256(content).hexdigest()
                doc_id = "doc-" + digest([self.project_id, self.kb, name])[:32]
                relative = f"originals/{sha}{Path(name).suffix.lower()}"
                original = self.inside(relative)
                original.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if original.exists():
                    if hashlib.sha256(original.read_bytes()).hexdigest() != sha:
                        raise AdapterError("Immutable original bytes were modified")
                else:
                    with original.open("xb") as stream:
                        stream.write(content)
                    original.chmod(0o400)
                record = {"id": doc_id, "project_id": self.project_id, "file_name": name,
                    "source_sha256": sha, "doc_version": "sha256:" + sha,
                    "status": "ACTIVE", "doc_type": "REFERENCE", "authority": "UNKNOWN",
                    "claim_role": "REFERENCE", "applicability": "", **supplied}
                documents[name] = {"logical_name": name, "source_path": relative, "record": record}
            if len(documents) > MAX_FILES:
                raise AdapterError("Knowledge folder exceeds document budget")
            if documents == state["documents"] and state["current_generation"]:
                self.snapshot(state["current_generation"])
                return state["current_generation"]
            return self._publish(state, documents, declaration)

    def import_folder(self, folder, *, metadata=None, declaration="Synthetic host folder", mirror=True):
        folder = Path(folder)
        if folder.is_symlink() or not folder.is_dir():
            raise AdapterError("Input folder must be an actual local directory")
        folder = folder.resolve()
        if folder.is_relative_to(self.root) or self.root.is_relative_to(folder):
            raise AdapterError("Input folder and store must be separate")
        files = {}
        # Every encountered symlink fails rather than following it into another KB.
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise AdapterError("Input folder contains symbolic link")
            if path.is_file():
                name = logical_name(path.relative_to(folder).as_posix())
                if path.stat().st_size > MAX_FILE_BYTES or len(files) >= MAX_FILES:
                    raise AdapterError("Input folder exceeds exploration budget")
                files[name] = path.read_bytes()
        return self.upload_files(files, metadata=metadata, declaration=declaration, replace_all=mirror)

    def delete_document(self, document_id, *, declaration="Synthetic host removal"):
        checked_id(document_id)
        with self.lock():
            state = self._state()
            documents = {n: d for n, d in state["documents"].items() if d["record"]["id"] != document_id}
            if len(documents) == len(state["documents"]):
                raise AdapterError("Unknown current document")
            return self._publish(state, documents, declaration)

    def _publish(self, state, documents, declaration):
        payload = {"schema_version": "openkb-original-snapshot/v1", "project_id": self.project_id,
            "kb": self.kb, "as_of": self.as_of.isoformat(), "revision": state["revision"] + 1,
            "previous_generation": state["current_generation"],
            "documents": sorted(documents.values(), key=lambda d: d["record"]["id"])}
        generation = "generation-" + digest(payload)
        target = self.inside("snapshots/" + generation)
        if not target.exists():
            stage = self.inside(".staging/" + uuid.uuid4().hex)
            stage.mkdir(parents=True, mode=0o700)
            try:
                entries = [{"source_path": str(self.inside(d["source_path"])), "record": d["record"]}
                           for d in payload["documents"]]
                if entries:
                    input_path = stage / "input.json"
                    write_json(input_path, {"schema_version": "reqpilot-evidence-input/v1",
                        "project_id": self.project_id, "knowledge_base_version": generation,
                        "as_of": self.as_of.isoformat(), "documents": entries,
                        "declaration": {"origin": "HOST_REGISTERED", "reference": declaration}})
                    register(SimpleNamespace(input=input_path, output_dir=stage / "native"))
                    catalog = verify_catalog(stage / "native/knowledge-catalog.json")
                    input_path.unlink()
                else:
                    empty = {"schema_version": "project-knowledge-facts/v1", "project_id": self.project_id,
                        "knowledge_base_version": generation, "evidence_catalog": {}, "documents": [],
                        "gaps": [], "selection": "EXPLICIT_DOCUMENTS"}
                    empty["catalog_sha256"] = digest(empty)
                    write_json(stage / "native/knowledge-catalog.json", empty)
                    catalog = verify_catalog(stage / "native/knowledge-catalog.json")
                write_json(stage / "manifest.json", sealed({**payload, "generation": generation,
                    "catalog_sha256": catalog["catalog_sha256"],
                    "registration_method": "REAL_REQPILOTX_REGISTER" if entries else "EMPTY_NO_EVIDENCE"}))
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                stage.rename(target)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        self.snapshot(generation)
        write_json(self.state_path, sealed({"schema_version": state["schema_version"],
            "project_id": self.project_id, "kb": self.kb,
            "current_generation": generation, "revision": payload["revision"], "documents": documents}), replace=True)
        return generation

    def snapshot(self, generation=None):
        generation = generation or self.current_generation
        if not isinstance(generation, str) or not GEN.fullmatch(generation):
            raise AdapterError("Invalid or absent knowledge generation")
        path = self.inside("snapshots/" + generation)
        try:
            if any(p.is_symlink() for p in path.rglob("*")):
                raise AdapterError("Snapshot contains an unsafe symbolic link")
            manifest = unseal(json.loads(self.inside(path.relative_to(self.root) / "manifest.json").read_text()))
            payload = {k: manifest[k] for k in ("schema_version", "project_id", "kb", "as_of", "revision", "previous_generation", "documents")}
            if (manifest["project_id"] != self.project_id or manifest["kb"] != self.kb
                    or "generation-" + digest(payload) != generation):
                raise AdapterError("Snapshot identity or namespace mismatch")
            for document in manifest["documents"]:
                original = self.inside(document["source_path"])
                if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != document["record"]["source_sha256"]:
                    raise AdapterError("Immutable manifest original bytes were modified")
            catalog = verify_catalog(self.inside(path.relative_to(self.root) / "native/knowledge-catalog.json"))
            if (catalog["catalog_sha256"] != manifest["catalog_sha256"]
                    or catalog["knowledge_base_version"] != generation
                    or catalog["project_id"] != self.project_id):
                raise AdapterError("Snapshot catalog does not match original manifest")
            return manifest, catalog
        except (ValueError, OSError, KeyError) as exc:
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError("Native original registration failed verification") from exc

    def reference_from_fact(self, fact_id, *, generation=None, quote=None):
        manifest, catalog = self.snapshot(generation)
        fact = catalog["evidence_catalog"].get(fact_id)
        if not fact:
            raise AdapterError("Unknown native fact")
        quote = fact["text"] if quote is None else quote
        if not isinstance(quote, str) or not quote.strip() or quote not in fact["text"]:
            raise AdapterError("Quote must be a nonempty exact contiguous original excerpt")
        start = fact["text"].find(quote)
        return {"schema_version": "openkb-original-reference/v1", "project_id": self.project_id,
            "kb": self.kb, "generation": manifest["generation"], "document_id": fact["document_id"],
            "source_sha256": fact["source_sha256"], "fact_id": fact_id,
            "source_ref": deepcopy(fact["source_ref"]), "quote_start": start,
            "quote_end": start + len(quote), "excerpt": quote,
            "text_sha256": hashlib.sha256(quote.encode()).hexdigest()}

    def resolve_reference(self, reference, snapshot_id=None, *, require_current=True):
        if not isinstance(reference, dict) or reference.get("schema_version") != "openkb-original-reference/v1":
            raise AdapterError("Structured original reference is required")
        if set(reference) != {"schema_version", "project_id", "kb", "generation", "document_id", "source_sha256",
                "fact_id", "source_ref", "quote_start", "quote_end", "excerpt", "text_sha256"}:
            raise AdapterError("Unexpected structured reference fields")
        generation = snapshot_id or reference.get("generation")
        if (reference.get("project_id") != self.project_id or reference.get("kb") != self.kb
                or reference.get("generation") != generation):
            raise AdapterError("Cross-project/KB or generation reference rejected")
        if require_current and generation != self.current_generation:
            raise AdapterError("Evidence generation is stale; review required")
        manifest, catalog = self.snapshot(generation)
        fact = catalog["evidence_catalog"].get(reference.get("fact_id"))
        if not fact:
            raise AdapterError("Fabricated or unmapped native fact reference")
        if (reference.get("document_id") != fact["document_id"]
                or reference.get("source_sha256") != fact["source_sha256"]
                or reference.get("source_ref") != fact["source_ref"]):
            raise AdapterError("Original identity, hash or locator changed")
        start, end = reference.get("quote_start"), reference.get("quote_end")
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(fact["text"])
                or reference.get("excerpt") != fact["text"][start:end]
                or reference.get("text_sha256") != hashlib.sha256(fact["text"][start:end].encode()).hexdigest()):
            raise AdapterError("Forged excerpt or quote offsets")
        if not fact.get("eligible"):
            raise AdapterError("Original is not lifecycle-eligible")
        record = next(d["record"] for d in manifest["documents"] if d["record"]["id"] == fact["document_id"])
        if require_current and not evaluate_knowledge_evidence_eligibility(record, as_of=self.as_of).allowed:
            raise AdapterError("Original has expired or is not current lifecycle-eligible")
        evidence_id = "original-evidence-" + digest(reference)
        siblings = [deepcopy(f) for f in catalog["evidence_catalog"].values() if f["document_id"] == fact["document_id"]]
        if len(siblings) > 256 or len(json.dumps(siblings, ensure_ascii=False).encode()) > 1024 * 1024:
            raise AdapterError("Original context exceeds budget; require a narrower context-aware native locator")
        gaps = [deepcopy(g) for g in catalog["gaps"] if g.get("document_id") == fact["document_id"]]
        return {"evidence_id": evidence_id, "evidence_contract_version": "evidence-v2",
            "project_id": self.project_id, "kb": self.kb, "generation": manifest["generation"],
            "source_doc_id": fact["document_id"], "source_doc_name": fact["document_name"],
            "source_doc_version": fact["document_version"], "source_sha256": fact["source_sha256"],
            "fact_id": fact["fact_id"], "source_ref": deepcopy(fact["source_ref"]),
            "source_location": json.dumps(fact["source_ref"], sort_keys=True, ensure_ascii=False),
            "excerpt": reference["excerpt"], "content_hash": compute_evidence_content_hash(reference["excerpt"]),
            "authority": fact["authority"], "authority_verified": fact["authority_verified"],
            "claim_role": fact["claim_role"], "retrieval_role": fact["role"],
            "applicability": fact["applicability"], "native_fact_context": deepcopy(fact),
            "document_context": siblings, "native_capture_gaps": gaps,
            "excerpt_scope": "ENTIRE_NATIVE_FACT" if start == 0 and end == len(fact["text"]) else "PARTIAL_NATIVE_FACT",
            "semantic_coverage": "NOT_ASSESSED", "relevance": 0.0,
            "provenance_validation": "RECAPTURED_FROM_IMMUTABLE_ORIGINAL",
            "human_acceptance": "NOT_CLAIMED", "reference": deepcopy(reference)}

    def export_openkb_input(self, generation=None):
        """Create immutable input projection for future real OpenKB ingestion.

        No compiler/service is called here. Never alter the upstream checkout.
        """
        manifest, catalog = self.snapshot(generation)
        if any(not evaluate_knowledge_evidence_eligibility(d["record"], as_of=self.as_of).allowed
               for d in manifest["documents"]):
            raise AdapterError("Do not compile an ineligible original into the current OpenKB input")
        projection = self.inside("openkb-input/" + manifest["generation"])
        if projection.exists():
            stored = unseal(json.loads(self.inside(projection.relative_to(self.root) / "projection.json").read_text()))
            if (stored.get("project_id") != self.project_id or stored.get("kb") != self.kb
                    or stored.get("generation") != manifest["generation"]
                    or stored.get("catalog_sha256") != catalog["catalog_sha256"]):
                raise AdapterError("OpenKB input projection namespace mismatch")
            for item in stored["documents"]:
                path = self.inside(projection.relative_to(self.root) / item["file"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                    raise AdapterError("OpenKB input projection changed")
            return stored
        projection.mkdir(parents=True, mode=0o700)
        records = []
        for doc in manifest["documents"]:
            doc_id = doc["record"]["id"]
            filename = doc_id + Path(doc["logical_name"]).suffix.lower()
            source = self.inside(doc["source_path"])
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != doc["record"]["source_sha256"]:
                raise AdapterError("Original projection bytes changed")
            target = projection / filename
            target.write_bytes(data)
            target.chmod(0o400)
            records.append({"document_id": doc_id, "logical_name": doc["logical_name"],
                "file": filename, "sha256": doc["record"]["source_sha256"],
                "suggested_openkb_doc_name": doc_id})
        value = sealed({"schema_version": "openkb-original-input-projection/v1",
            "project_id": self.project_id, "kb": self.kb, "generation": manifest["generation"],
            "catalog_sha256": catalog["catalog_sha256"], "documents": records,
            "openkb_execution": "NOT_RUN", "upstream_generation_attestation": "NOT_AVAILABLE"})
        write_json(projection / "projection.json", value)
        return value

    def prepare_openkb_workspace(self, generation=None):
        """Allocate a private, generation-specific future compiler workspace.

        Compiler output is not an attestation of evidence; original read tools
        remain authoritative. No existing foreign Wiki can be supplied to the agent.
        """
        projection = self.export_openkb_input(generation)
        generation = projection["generation"]
        directory = self.inside("openkb-runs/" + generation)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        binding = sealed({"schema_version": "openkb-host-workspace-binding/v1",
            "project_id": self.project_id, "kb": self.kb, "generation": generation,
            "original_projection_sha256": projection["record_sha256"],
            "wiki_relative_path": "openkb-runs/" + generation + "/wiki",
            "binding_validation": "HOST_OWNED_PATH_ONLY_NOT_UPSTREAM_ATTESTATION"})
        path = self.inside(directory.relative_to(self.root) / "binding.json")
        if path.exists():
            if unseal(json.loads(path.read_text())) != binding:
                raise AdapterError("OpenKB workspace binding changed")
        else:
            write_json(path, binding)
        return binding
