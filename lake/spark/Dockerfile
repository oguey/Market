FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends default-jre-headless \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home app
# Connecteur S3 (S3A) de Hadoop, version alignee sur celle embarquee par PySpark 3.5.x
ADD https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/3.3.4/hadoop-aws-3.3.4.jar /usr/local/lib/python3.11/site-packages/pyspark/jars/
ADD https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.12.262/aws-java-sdk-bundle-1.12.262.jar /usr/local/lib/python3.11/site-packages/pyspark/jars/
RUN chmod -R a+r /usr/local/lib/python3.11/site-packages/pyspark/jars/
WORKDIR /app
COPY *.py ./
USER app
CMD ["python", "staging.py"]
