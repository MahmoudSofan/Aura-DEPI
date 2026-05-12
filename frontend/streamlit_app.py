"""Streamlit frontend for Aura.

Run from repo root:
    streamlit run frontend/streamlit_app.py

Configure backend URL via env (default http://localhost:8000):
    AURA_API_BASE=http://api:8000  (when running inside compose)
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

PLATFORMS = ["facebook", "instagram", "tiktok", "twitter", "linkedin", "youtube"]
API_BASE = os.getenv("AURA_API_BASE", "http://localhost:8000")
HTTP_TIMEOUT = 30.0


def init_state() -> None:
    if "run_id" not in st.session_state:
        st.session_state.run_id = None
    if "selected_brand_id" not in st.session_state:
        st.session_state.selected_brand_id = None


# ---------------------------------------------------------------------------
# API client helpers (call /api/v1).
# ---------------------------------------------------------------------------


def submit_campaign(request: dict[str, str]) -> dict[str, str]:
    response = httpx.post(f"{API_BASE}/api/v1/campaigns", json=request, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return dict(response.json())


def get_campaign(run_id: str) -> dict[str, Any]:
    response = httpx.get(f"{API_BASE}/api/v1/campaigns/{run_id}", timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return dict(response.json())


def list_brands() -> list[dict[str, Any]]:
    response = httpx.get(f"{API_BASE}/api/v1/brands", timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    body = response.json()
    return list(body) if isinstance(body, list) else []


def create_brand(display_name: str) -> dict[str, Any]:
    response = httpx.post(
        f"{API_BASE}/api/v1/brands",
        json={"display_name": display_name},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return dict(response.json())


def delete_brand(brand_id: str) -> None:
    response = httpx.delete(f"{API_BASE}/api/v1/brands/{brand_id}", timeout=HTTP_TIMEOUT)
    response.raise_for_status()


def list_documents(brand_id: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{API_BASE}/api/v1/brands/{brand_id}/documents",
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    return list(body) if isinstance(body, list) else []


def upload_document(brand_id: str, file_name: str, file_bytes: bytes) -> dict[str, Any]:
    response = httpx.post(
        f"{API_BASE}/api/v1/brands/{brand_id}/documents",
        files={"file": (file_name, file_bytes)},
        timeout=HTTP_TIMEOUT * 2,
    )
    response.raise_for_status()
    return dict(response.json())


# ---------------------------------------------------------------------------
# Screens.
# ---------------------------------------------------------------------------


def screen_brands() -> None:
    st.header("1. Brands")
    st.caption(
        "Create, list, and delete brands. Deleting a brand cascades to its documents and runs."
    )

    with st.form("create_brand"):
        name = st.text_input("Display name", placeholder="ACME Footwear")
        submitted = st.form_submit_button("Create brand", type="primary")
        if submitted:
            if not name.strip():
                st.error("Display name is required.")
            else:
                try:
                    brand = create_brand(name.strip())
                    st.success(f"Created brand `{brand['id']}` ({brand['display_name']}).")
                except httpx.HTTPStatusError as exc:
                    st.error(f"Create failed ({exc.response.status_code}): {exc.response.text}")
                except httpx.HTTPError as exc:
                    st.error(f"Create failed: {exc}")

    st.divider()
    st.subheader("Existing brands")
    try:
        brands = list_brands()
    except httpx.HTTPError as exc:
        st.error(f"Failed to list brands: {exc}")
        return

    if not brands:
        st.info("No brands yet. Create one above.")
        return

    for brand in brands:
        with st.container(border=True):
            cols = st.columns([3, 2, 1])
            with cols[0]:
                st.markdown(f"**{brand['display_name']}**")
                st.caption(f"id: `{brand['id']}`")
            with cols[1]:
                st.caption(f"created: {brand['created_at']}")
            with cols[2]:
                if st.button("Delete", key=f"del-{brand['id']}", type="secondary"):
                    try:
                        delete_brand(brand["id"])
                        st.success(f"Deleted `{brand['id']}`.")
                        st.rerun()
                    except httpx.HTTPStatusError as exc:
                        st.error(f"Delete failed ({exc.response.status_code}): {exc.response.text}")
                    except httpx.HTTPError as exc:
                        st.error(f"Delete failed: {exc}")


def _brand_selector(label: str, *, key: str) -> str | None:
    try:
        brands = list_brands()
    except httpx.HTTPError as exc:
        st.error(f"Failed to list brands: {exc}")
        return None
    if not brands:
        st.info("No brands yet — create one on the Brands screen.")
        return None
    options = {f"{b['display_name']} ({b['id']})": b["id"] for b in brands}
    label_key = st.selectbox(label, options=list(options.keys()), key=key)
    return options[label_key]


def screen_documents() -> None:
    st.header("2. Documents")
    st.caption("Upload PDF / DOCX / TXT / MD documents to ground campaigns in brand facts.")

    brand_id = _brand_selector("Brand", key="docs-brand-select")
    if brand_id is None:
        return
    st.session_state.selected_brand_id = brand_id

    uploaded = st.file_uploader(
        "Document",
        type=["pdf", "docx", "txt", "md"],
        accept_multiple_files=False,
    )
    if uploaded is not None and st.button("Ingest", type="primary"):
        try:
            doc = upload_document(brand_id, uploaded.name, uploaded.getvalue())
            st.success(
                f"Ingested `{doc['original_filename']}` "
                f"({doc['chunk_count']} chunks, parse_status={doc['parse_status']})."
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 409:
                st.warning("Duplicate: this exact file has already been ingested for this brand.")
            elif code == 413:
                st.error("File exceeds the 50 MB cap.")
            elif code == 415:
                st.error("Unsupported file type. Use PDF, DOCX, TXT, or MD.")
            elif code == 422:
                st.error(f"Could not parse: {exc.response.text}")
            else:
                st.error(f"Upload failed ({code}): {exc.response.text}")
        except httpx.HTTPError as exc:
            st.error(f"Upload failed: {exc}")

    st.divider()
    st.subheader("Ingested documents")
    try:
        docs = list_documents(brand_id)
    except httpx.HTTPError as exc:
        st.error(f"Failed to list documents: {exc}")
        return
    if not docs:
        st.info("No documents yet.")
        return
    rows = [
        {
            "filename": d["original_filename"],
            "format": d["format"],
            "chunks": d["chunk_count"],
            "status": d["parse_status"],
            "size": d["byte_size"],
            "created": d["created_at"],
        }
        for d in docs
    ]
    st.dataframe(rows, use_container_width=True)


def screen_campaign() -> None:
    st.header("3. Campaign request")
    st.caption("Describe the campaign brief and pick a platform.")

    brand_id = _brand_selector("Brand", key="campaign-brand-select")
    if brand_id is None:
        return
    st.session_state.selected_brand_id = brand_id

    brief = st.text_area(
        "Brief",
        height=160,
        placeholder="Launch summer sneaker line targeting Gen Z runners...",
    )
    platform = st.selectbox("Platform", PLATFORMS, index=PLATFORMS.index("instagram"))
    target_audience = st.text_input(
        "Target audience",
        placeholder="18-24, urban, fitness-curious",
    )

    can_submit = bool(brief.strip() and target_audience.strip())
    if st.button("Generate", type="primary", disabled=not can_submit):
        request = {
            "brief": brief,
            "platform": platform,
            "brand_id": brand_id,
            "target_audience": target_audience,
        }
        try:
            with st.spinner("Submitting..."):
                ack = submit_campaign(request)
            st.session_state.run_id = ack["run_id"]
            st.success(
                f"Submitted (status: **{ack['status']}**). Open the Results screen. "
                f"run_id: `{ack['run_id']}`"
            )
        except httpx.HTTPStatusError as exc:
            st.error(f"Submit rejected ({exc.response.status_code}): {exc.response.text}")
        except httpx.HTTPError as exc:
            st.error(f"Submit failed: {exc}")


def _render_status_badge(status: str) -> None:
    if status == "queued":
        st.info("Status: **queued** — waiting for a runner slot.")
    elif status == "running":
        st.info("Status: **running**")
    elif status == "done":
        st.success("Status: **done**")
    elif status == "failed":
        st.error("Status: **failed**")
    else:
        st.info(f"Status: **{status}**")


def _render_trace(trace: list[dict[str, Any]]) -> None:
    if not trace:
        st.caption("No stage trace yet.")
        return
    rows = [
        {
            "stage": entry["stage"],
            "attempt": entry["attempt"],
            "status": entry["status"],
            "duration_ms": entry["duration_ms"],
            "error": entry.get("error_message") or "",
        }
        for entry in trace
    ]
    st.dataframe(rows, use_container_width=True)


def _render_done(data: dict[str, Any]) -> None:
    output = data["output"]
    col_copy, col_image = st.columns([3, 2])

    with col_copy:
        st.subheader("Ad copy")
        ad = output["ad_copy"]
        st.markdown(f"### {ad['headline']}")
        st.write(ad["primary_text"])
        st.button(ad["cta"], type="primary", disabled=True)
        st.caption(f"Platform: {ad['platform']}")

    with col_image:
        st.subheader("Image")
        # output["image_url"] is API-relative (FR-023) — load via the API.
        full_url = f"{API_BASE}{output['image_url']}"
        st.image(full_url, caption=f"{output['image_width']}x{output['image_height']}")

    st.divider()
    st.subheader("Critic score")
    score = output["score"]

    c1, c2 = st.columns([1, 2])
    with c1:
        st.metric("Overall", f"{score['overall']:.2f}")
        if score["passed"]:
            st.success("PASSED")
        else:
            st.error("BELOW THRESHOLD")
    with c2:
        st.write("**Breakdown**")
        for k, v in score["breakdown"].items():
            st.progress(float(v), text=f"{k}: {v:.2f}")

    st.write("**Feedback**")
    st.info(score["feedback"])


def screen_results() -> None:
    st.header("4. Results")

    run_id = st.session_state.run_id
    manual_id = st.text_input("Or paste a run id:", placeholder="01HX...")
    target = manual_id.strip() or run_id

    if not target:
        st.info("No campaign generated yet. Go to the Campaign request screen.")
        return

    st.caption(f"run_id: `{target}`")
    if st.button("Refresh status"):
        st.rerun()

    try:
        data = get_campaign(target)
    except httpx.HTTPStatusError as exc:
        st.error(f"Status fetch rejected ({exc.response.status_code}): {exc.response.text}")
        return
    except httpx.HTTPError as exc:
        st.error(f"Failed to fetch status: {exc}")
        return

    status = data.get("status", "unknown")
    progress = float(data.get("progress", 0.0))
    _render_status_badge(status)
    st.progress(progress)

    if status == "failed":
        st.error(
            f"Failed at stage **{data.get('failed_stage') or 'n/a'}**: "
            f"{data.get('failed_reason') or 'unspecified'}"
        )
    elif status == "done" and data.get("output"):
        _render_done(data)

    with st.expander("Per-stage trace", expanded=(status in ("running", "queued"))):
        _render_trace(data.get("trace") or [])


SCREENS = {
    "1. Brands": screen_brands,
    "2. Documents": screen_documents,
    "3. Campaign request": screen_campaign,
    "4. Results": screen_results,
}


def main() -> None:
    st.set_page_config(page_title="Aura", layout="wide")
    init_state()

    st.sidebar.title("Aura")
    st.sidebar.caption(f"API: `{API_BASE}`")
    choice = st.sidebar.radio("Screen", list(SCREENS.keys()))
    SCREENS[choice]()


if __name__ == "__main__":
    main()
