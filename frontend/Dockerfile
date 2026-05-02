FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir "streamlit>=1.32" "httpx>=0.27"

EXPOSE 8501

CMD ["streamlit", "run", "streamlit_app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--browser.gatherUsageStats=false"]
