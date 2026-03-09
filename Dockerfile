FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

# Copy app code
COPY scanner.py .
COPY web/ web/

EXPOSE 5000

# Run with gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "0", "web.app:app"]
