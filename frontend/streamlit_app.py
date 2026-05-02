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
    if "uploaded_docs" not in st.session_state:
        st.session_state.uploaded_docs = []
    if "run_id" not in st.session_state:
        st.session_state.run_id = None


def upload_document(filename: str, file_bytes: bytes, brand: str) -> dict[str, str]:
    """POST /api/documents/upload."""
    files = {"file": (filename, file_bytes)}
    response = httpx.post(f"{API_BASE}/api/documents/upload", files=files, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return {"doc_id": response.json()["doc_id"], "filename": filename, "brand": brand}


def submit_campaign(request: dict[str, str]) -> str:
    """POST /api/campaigns/generate -> run_id."""
    response = httpx.post(
        f"{API_BASE}/api/campaigns/generate", json=request, timeout=HTTP_TIMEOUT
    )
    response.raise_for_status()
    return str(response.json()["run_id"])


def get_campaign_status(run_id: str) -> dict[str, Any]:
    """GET /api/campaigns/{run_id}/status."""
    response = httpx.get(
        f"{API_BASE}/api/campaigns/{run_id}/status", timeout=HTTP_TIMEOUT
    )
    response.raise_for_status()
    return dict(response.json())


def screen_documents() -> None:
    st.header("1. Documents")
    st.caption("Upload brand documents — voice guides, past campaigns, product specs.")

    brand = st.text_input("Brand name", placeholder="ACME Sneakers")
    files = st.file_uploader(
        "Upload documents",
        accept_multiple_files=True,
        type=["pdf", "txt", "md", "docx"],
    )

    if st.button("Upload", type="primary", disabled=not (brand and files)):
        successes = 0
        for f in files:
            try:
                record = upload_document(f.name, f.getvalue(), brand)
                st.session_state.uploaded_docs.append(record)
                successes += 1
            except httpx.HTTPError as e:
                st.error(f"Upload failed for {f.name}: {e}")
        if successes:
            st.success(f"Uploaded {successes} document(s) for brand '{brand}'.")

    st.divider()
    st.subheader("Uploaded documents")
    if not st.session_state.uploaded_docs:
        st.info("No documents uploaded yet.")
    else:
        st.dataframe(st.session_state.uploaded_docs, use_container_width=True)


def screen_campaign() -> None:
    st.header("2. Campaign request")
    st.caption("Describe the campaign brief and pick a platform.")

    brief = st.text_area(
        "Brief",
        height=160,
        placeholder="Launch summer sneaker line targeting Gen Z runners...",
    )
    platform = st.selectbox("Platform", PLATFORMS, index=PLATFORMS.index("instagram"))

    known_brands = sorted({doc["brand"] for doc in st.session_state.uploaded_docs})
    placeholder = known_brands[0] if known_brands else "brand_acme_001"
    brand_id = st.text_input(
        "Brand ID",
        placeholder=placeholder,
        help="Use one of your uploaded brand names or any string identifier.",
    )

    target_audience = st.text_input(
        "Target audience",
        placeholder="18-24, urban, fitness-curious",
    )

    can_submit = bool(brief.strip() and brand_id.strip() and target_audience.strip())
    if st.button("Generate", type="primary", disabled=not can_submit):
        request = {
            "brief": brief,
            "platform": platform,
            "brand_id": brand_id,
            "target_audience": target_audience,
        }
        try:
            with st.spinner("Submitting..."):
                run_id = submit_campaign(request)
            st.session_state.run_id = run_id
            st.success(f"Submitted. Open the Results screen. run_id: `{run_id}`")
        except httpx.HTTPError as e:
            st.error(f"Submit failed: {e}")


def render_campaign_result(result: dict[str, Any]) -> None:
    col_copy, col_image = st.columns([3, 2])

    with col_copy:
        st.subheader("Ad copy")
        ad = result["ad_copy"]
        st.markdown(f"### {ad['headline']}")
        st.write(ad["primary_text"])
        st.button(ad["cta"], type="primary", disabled=True)
        st.caption(f"Platform: {ad['platform']}")

    with col_image:
        st.subheader("Image")
        img = result["image"]
        st.image(img["path"], caption=f"{img['dimensions'][0]}x{img['dimensions'][1]}")

    st.divider()
    st.subheader("Critic score")
    score = result["score"]

    c1, c2 = st.columns([1, 2])
    with c1:
        st.metric("Overall", f"{score['overall']:.2f}")
        if score["passed"]:
            st.success("PASSED")
        else:
            st.error("FAILED")

    with c2:
        st.write("**Breakdown**")
        for k, v in score["breakdown"].items():
            st.progress(v, text=f"{k}: {v:.2f}")

    st.write("**Feedback**")
    st.info(score["feedback"])


def screen_results() -> None:
    st.header("3. Results")

    run_id = st.session_state.run_id
    if not run_id:
        st.info("No campaign generated yet. Go to the Campaign request screen.")
        return

    st.caption(f"run_id: `{run_id}`")
    if st.button("Refresh status"):
        st.rerun()

    try:
        data = get_campaign_status(run_id)
    except httpx.HTTPError as e:
        st.error(f"Failed to fetch status: {e}")
        return

    status = data.get("status", "unknown")
    progress = float(data.get("progress", 0.0))
    result = data.get("result")

    if status == "completed" and result:
        render_campaign_result(result)
    elif status == "failed":
        st.error(f"Run failed: {data.get('error', 'unknown')}")
    else:
        st.info(f"Status: **{status}** ({progress:.0%})")
        st.progress(progress)
        st.caption(
            "Pipeline still running. The LangGraph isn't wired yet, "
            "so runs stay 'running' indefinitely until that lands."
        )


SCREENS = {
    "1. Documents": screen_documents,
    "2. Campaign request": screen_campaign,
    "3. Results": screen_results,
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
