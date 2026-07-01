FROM python:3.10-slim

# Install system-level tzdata to ensure timezones like America/New_York are fully supported.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Optional: Set the timezone to KST (Asia/Seoul) for localized logging inside the container.
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all source files
COPY . .

# Run the trading bot
CMD ["python", "main.py"]
