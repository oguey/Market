FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends default-jre-headless \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home app
# Connecteur S3 (S3A) de Hadoop, version alignee sur celle embarquee par PySpark 3.5.x
ADD https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/3.3.4/hadoop-aws-3.3.4.jar /usr/local/lib/python3.11/site-packages/pyspark/jars/
ADD https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.12.262/aws-java-sdk-bundle-1.12.262.jar /usr/local/lib/python3.11/site-packages/pyspark/jars/
# Delta Lake 3.2.1 (compatible Spark 3.5.x) : tables transactionnelles au-dessus de Parquet
ADD https://repo1.maven.org/maven2/io/delta/delta-spark_2.12/3.2.1/delta-spark_2.12-3.2.1.jar /usr/local/lib/python3.11/site-packages/pyspark/jars/
ADD https://repo1.maven.org/maven2/io/delta/delta-storage/3.2.1/delta-storage-3.2.1.jar /usr/local/lib/python3.11/site-packages/pyspark/jars/
# Connecteur Kafka pour Spark Structured Streaming (versions alignees sur Spark 3.5.3)
ADD https://repo1.maven.org/maven2/org/apache/spark/spark-sql-kafka-0-10_2.12/3.5.3/spark-sql-kafka-0-10_2.12-3.5.3.jar /usr/local/lib/python3.11/site-packages/pyspark/jars/
ADD https://repo1.maven.org/maven2/org/apache/spark/spark-token-provider-kafka-0-10_2.12/3.5.3/spark-token-provider-kafka-0-10_2.12-3.5.3.jar /usr/local/lib/python3.11/site-packages/pyspark/jars/
ADD https://repo1.maven.org/maven2/org/apache/kafka/kafka-clients/3.4.1/kafka-clients-3.4.1.jar /usr/local/lib/python3.11/site-packages/pyspark/jars/
ADD https://repo1.maven.org/maven2/org/apache/commons/commons-pool2/2.11.1/commons-pool2-2.11.1.jar /usr/local/lib/python3.11/site-packages/pyspark/jars/
RUN chmod -R a+r /usr/local/lib/python3.11/site-packages/pyspark/jars/
# Checkpoints du streaming (volume Docker : le job reprend la ou il s'est arrete)
RUN mkdir /checkpoints && chown app /checkpoints
WORKDIR /app
COPY *.py ./
USER app
CMD ["python", "staging.py"]
