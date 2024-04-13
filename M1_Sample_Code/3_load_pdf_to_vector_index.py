# Databricks notebook source
# MAGIC %md
# MAGIC # Example PDF Parsing Pipeline
# MAGIC
# MAGIC This is an example notebook that provides a **starting point** to build a data pipeline that loads, parses, chunks, and embeds PDF files from a UC Volume into a Databricks Vector Search Index.  
# MAGIC
# MAGIC Getting the right parsing and chunk size requires iteration and a working knowledge of your data - this pipeline is easy to adapt and tweak in order to add more advanced logic.
# MAGIC
# MAGIC Limitations: 
# MAGIC - This pipeline resets the index every time, mirroring the index to the files in the UC Volume.  A future iteration will only update added/changed/removed files.
# MAGIC - Splitting based on tokens requires a cluster with internet access.  If you do not have internet access on your cluster, adjust the gold parsing step.
# MAGIC - Can't change column names in the Vector Index after the tables are initially created - to change column names, delete the Vector Index and re-sync.

# COMMAND ----------

# MAGIC %md
# MAGIC # How To / Getting Started
# MAGIC
# MAGIC 1. To get started, press `Run All`.  
# MAGIC 2. You will be alerted to any configuration settings you need to config or issues you need to resolve.  
# MAGIC 3. After you resolve an issue or set a configuration setting, press `Run All` again to verify your changes.  
# MAGIC   *Note: Dropdown configurations will take a few seconds to load the values.*
# MAGIC 4. Repeat until you don't get errors and press `Run All` a final time to execute the pipeline.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Install libraries & import packages

# COMMAND ----------

# MAGIC %pip install -U --quiet pypdf==4.1.0 databricks-sdk langchain==0.1.13
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import io
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, ResourceDoesNotExist
from databricks.sdk.service.vectorsearch import (
    EndpointType,
    DeltaSyncVectorIndexSpecRequest,
    VectorIndexType,
    EmbeddingSourceColumn,
    PipelineType,
    EndpointStatusState
)
import pyspark.sql.functions as func
from pyspark.sql.types import MapType, StringType
from pypdf import PdfReader
from langchain.text_splitter import RecursiveCharacterTextSplitter, CharacterTextSplitter
from pyspark.sql import Column
from pyspark.sql.types import *
from datetime import timedelta
from typing import List
import warnings

# Init workspace client
w = WorkspaceClient()

# Use optimizations if available
dbr_majorversion = int(spark.conf.get("spark.databricks.clusterUsageTags.sparkVersion").split(".")[0])
if dbr_majorversion >= 14:
  spark.conf.set("spark.sql.execution.pythonUDF.arrow.enabled", True)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Required configuration
# MAGIC
# MAGIC 1. Select a Vector Search endpoint
# MAGIC
# MAGIC If you do not have a Databricks Vector Search endpoint, follow these [steps](https://docs.databricks.com/en/generative-ai/create-query-vector-search.html#create-a-vector-search-endpoint) to create one.
# MAGIC
# MAGIC 2. Select UC Catalog, Schema, and UC Volume w/ PDF files.
# MAGIC
# MAGIC Note: By default, the bronze/silver/gold Delta Tables with parsed chunks will land into this same UC Catalog/Schema.  You can change this behavior below.

# COMMAND ----------

# DBTITLE 1,Databricks Vector Search Configuration
# # Get Vector Search Endpoints
# vector_search_endpoints_in_workspace = [item.name for item in w.vector_search_endpoints.list_endpoints() if item.endpoint_status.state == EndpointStatusState.ONLINE]
# if len(vector_search_endpoints_in_workspace) == 0:
#     raise Exception("No Vector Search Endpoints are online in this workspace.  Please follow the instructions here to create a Vector Search endpoint: https://docs.databricks.com/en/generative-ai/create-query-vector-search.html#create-a-vector-search-endpoint")

# # Create parameter
# dbutils.widgets.dropdown(
#     "vector_search_endpoint_name",
#     defaultValue="",
#     choices=vector_search_endpoints_in_workspace+[""],
#     label="#1 Select VS endpoint",
# )

# # Set local variable for use later
# vector_search_endpoint_name = dbutils.widgets.get("vector_search_endpoint_name")
vector_search_endpoint_name = "one-env-shared-endpoint-5"

# Validation
if vector_search_endpoint_name == '' or vector_search_endpoint_name is None:
    raise Exception("Please select a Vector Search endpoint to continue.")
else:
    print(f"Using `{vector_search_endpoint_name}` as the Vector Search endpoint.")

# # Get UC Catalog names
# uc_catalogs = [row.catalog for row in spark.sql("SHOW CATALOGS").collect()]
# dbutils.widgets.dropdown(
#     "uc_catalog_name",
#     defaultValue="",
#     choices=uc_catalogs + [""],
#     label="#2 Select UC Catalog",
# )

# uc_catalog_name = dbutils.widgets.get("uc_catalog_name")
uc_catalog_name = "jendarra_cat"

# # Get UC Schemas within the selected catalog
# if uc_catalog_name != "" and uc_catalog_name is not None:
#     spark.sql(f"USE CATALOG `{uc_catalog_name}`")

#     uc_schemas = [row.databaseName for row in spark.sql(f"SHOW SCHEMAS").collect()]
#     uc_schemas = [schema for schema in uc_schemas if schema != "__databricks_internal"]

#     dbutils.widgets.dropdown(
#         "uc_schema_name",
#         defaultValue="",
#         choices=[""] + uc_schemas,
#         label="#3 Select UC Schema",
#     )
# else:
#     dbutils.widgets.dropdown(
#         "uc_schema_name",
#         defaultValue="",
#         choices=[""],
#         label="#3 Select UC Schema",
#     )

# uc_schema_name = dbutils.widgets.get("uc_schema_name")
uc_schema_name = "pragster"

# Get UC Volumes within the selected catalog/schema
if uc_schema_name != "" and uc_schema_name is not None:
    spark.sql(f"USE CATALOG `{uc_catalog_name}`")
    spark.sql(f"USE SCHEMA `{uc_schema_name}`")
    uc_volumes = [row.volume_name for row in spark.sql(f"SHOW VOLUMES").collect()]

    dbutils.widgets.dropdown(
        "source_uc_volume",
        defaultValue="",
        choices=[""] + uc_volumes,
        label="#4 Select UC Volume w/ PDFs",
    )
else:
    dbutils.widgets.dropdown(
        "source_uc_volume",
        defaultValue="",
        choices=[""] + uc_volumes,
        label="#4 Select UC Volume w/ PDFs",
    )

source_uc_volume = f"/Volumes/{uc_catalog_name}/{uc_schema_name}/{dbutils.widgets.get('source_uc_volume')}"

# Validation
if (uc_catalog_name == "" or uc_catalog_name is None) or (
    uc_schema_name == "" or uc_schema_name is None
):
    raise Exception("Please select a UC Catalog & Schema to continue.")
else:
    print(f"Using `{uc_catalog_name}.{uc_schema_name}` as the UC Catalog / Schema.")

if source_uc_volume == "" or source_uc_volume is None:
    raise Exception("Please select a source UC Volume w/ PDF files to continue.")
else:
    print(
        f"Using {source_uc_volume} as the UC Volume Source."
    )

# COMMAND ----------

# MAGIC %md ## Optional: Configure parameters
# MAGIC
# MAGIC We suggest starting with the default values to verify the pipeline works end to end.  You'll need to tune these settings to optimize the retrieval quality for your data.
# MAGIC
# MAGIC When comparing multiple configurations (different chunking settings, embedding models, etc), we suggest adjusting the bronze/silver/gold names to indicate different versions.

# COMMAND ----------

# DBTITLE 1,Data Processing Workflow Manager
# Force this cell to re-run when these values are changed in the Notebook widgets
# uc_catalog_name = dbutils.widgets.get("uc_catalog_name")
uc_catalog_name = "jendarra_cat"
# uc_schema_name = dbutils.widgets.get("uc_schema_name")
uc_schema_name = "pragster"
volume_raw_name = dbutils.widgets.get("source_uc_volume")

# Defaults
BGE_CONTEXT_WINDOW_LENGTH_TOKENS = 512
CHUNK_SIZE_TOKENS = 425
CHUNK_OVERLAP_TOKENS = 75
DATABRICKS_FMAPI_BGE_ENDPOINT = "databricks-bge-large-en"
FMAPI_EMBEDDINGS_TASK = "llm/v1/embeddings"

bronze_raw_files_table_name = (
    f"{uc_catalog_name}.{uc_schema_name}.bronze_{volume_raw_name}_raw"
)
silver_parsed_files_table_name = (
    f"{uc_catalog_name}.{uc_schema_name}.silver_{volume_raw_name}_parsed"
)
gold_chunks_table_name = (
    f"{uc_catalog_name}.{uc_schema_name}.gold_{volume_raw_name}_chunked"
)
gold_chunks_index_name = (
    f"{uc_catalog_name}.{uc_schema_name}.gold_{volume_raw_name}_chunked_index"
)

print(f"Bronze Delta Table w/ raw files: `{bronze_raw_files_table_name}`")
print(f"Silver Delta Table w/ parsed files: `{silver_parsed_files_table_name}`")
print(f"Gold Delta Table w/ chunked files: `{gold_chunks_table_name}`")
print(f"Vector Search Index mirror of Gold Delta Table: `{gold_chunks_index_name}`")
print("--")

dbutils.widgets.text(
    "embedding_endpoint_name",
    DATABRICKS_FMAPI_BGE_ENDPOINT,
    label="Parameter: embedding endpoint",
)
embedding_endpoint_name = dbutils.widgets.get("embedding_endpoint_name")

try:
    w.serving_endpoints.get(embedding_endpoint_name)
except ResourceDoesNotExist as e:
    error = f"Model serving endpoint {embedding_endpoint_name} does not exist."
    if embedding_endpoint_name == DATABRICKS_FMAPI_BGE_ENDPOINT:
        error = (
            error
            + " This is likely because FMAPI is not available in your region.  To deploy the BGE embedding model using FMAPI, please see: https://docs.databricks.com/en/machine-learning/foundation-models/deploy-prov-throughput-foundation-model-apis.html#provisioned-throughput-serving-for-bge-model-notebook"
        )
    else:
        error = error + " Verify your endpoint is properly configured."
    raise Exception(error)

if w.serving_endpoints.get(embedding_endpoint_name).task != FMAPI_EMBEDDINGS_TASK:
    raise Exception(
        f"Your endpoint `{embedding_endpoint_name}` is not of type {FMAPI_EMBEDDINGS_TASK}.  Visit the Foundational Model APIs documentation to create a compatible endpoint: https://docs.databricks.com/en/machine-learning/foundation-models/index.html"
    )

print(f"Embedding model endpoint: `{embedding_endpoint_name}`")
print("--")
dbutils.widgets.text(
    "chunk_size_tokens", str(CHUNK_SIZE_TOKENS), label="Parameter: chunk size"
)
chunk_size_tokens = int(dbutils.widgets.get("chunk_size_tokens"))

dbutils.widgets.text(
    "chunk_overlap_tokens", str(CHUNK_OVERLAP_TOKENS), label="Parameter: chunk overlap"
)
chunk_overlap_tokens = int(dbutils.widgets.get("chunk_overlap_tokens"))

if (
    embedding_endpoint_name == DATABRICKS_FMAPI_BGE_ENDPOINT
    and (chunk_size_tokens + chunk_overlap_tokens) > BGE_CONTEXT_WINDOW_LENGTH_TOKENS
):
    print(
        f"WARNING: Your chunk configuration exceeds `{embedding_endpoint_name}` context window of {BGE_CONTEXT_WINDOW_LENGTH_TOKENS} tokens.  Embedding performance may be diminished since tokens past {BGE_CONTEXT_WINDOW_LENGTH_TOKENS} tokens are ignored by the embedding model."
    )
else:
    print(
        f"Using chunking parameters: chunk_size_tokens: {chunk_size_tokens}, chunk_overlap_tokens: {chunk_overlap_tokens}"
    )

# COMMAND ----------

# If you want to run this pipeline as a Job, remove the above 2 cells which implement the dropdown functionality.  Uncomment this code.

# # Defaults
# BGE_CONTEXT_WINDOW_LENGTH_TOKENS = 512
# CHUNK_SIZE_TOKENS = 425
# CHUNK_OVERLAP_TOKENS = 75
# DATABRICKS_FMAPI_BGE_ENDPOINT = "databricks-bge-large-en"
# FMAPI_EMBEDDINGS_TASK = "llm/v1/embeddings"

# # Vector Search Endpoint
# dbutils.widgets.text(
#     "vector_search_endpoint_name",
#     defaultValue="",
#     label="#1 VS endpoint",
# )
# vector_search_endpoint_name = dbutils.widgets.get("vector_search_endpoint_name")
# print("--")
# print(f"Using `{vector_search_endpoint_name}` as the Vector Search endpoint.")

# # UC Catalog
# dbutils.widgets.text(
#     "uc_catalog_name",
#     defaultValue="catalog_name",
#     label="#2 UC Catalog",
# )
# uc_catalog_name = dbutils.widgets.get("uc_catalog_name")

# # UC Schema
# dbutils.widgets.text(
#     "uc_schema_name",
#     defaultValue="",
#     label="#3 UC Schema",
# )
# uc_schema_name = dbutils.widgets.get("uc_schema_name")
# print("--")
# print(f"Using `{uc_catalog_name}.{uc_schema_name}` as the UC Catalog / Schema.")

# # UC Volume
# dbutils.widgets.text(
#     "source_uc_volume",
#     defaultValue="volume_name",
#     label="#4 UC Volume w/ PDFs",
# )

# volume_raw_name = dbutils.widgets.get("source_uc_volume")

# source_uc_volume = f"/Volumes/{uc_catalog_name}/{uc_schema_name}/{dbutils.widgets.get('source_uc_volume')}"
# print("--")
# print(f"Using {source_uc_volume} as the UC Volume Source.")

# bronze_raw_files_table_name = (
#     f"{uc_catalog_name}.{uc_schema_name}.bronze_{volume_raw_name}_raw"
# )
# silver_parsed_files_table_name = (
#     f"{uc_catalog_name}.{uc_schema_name}.silver_{volume_raw_name}_parsed"
# )
# gold_chunks_table_name = (
#     f"{uc_catalog_name}.{uc_schema_name}.gold_{volume_raw_name}_chunked"
# )
# gold_chunks_index_name = (
#     f"{uc_catalog_name}.{uc_schema_name}.gold_{volume_raw_name}_chunked_index"
# )
# print("--")
# print(f"Bronze Delta Table w/ raw files: `{bronze_raw_files_table_name}`")
# print(f"Silver Delta Table w/ parsed files: `{silver_parsed_files_table_name}`")
# print(f"Gold Delta Table w/ chunked files: `{gold_chunks_table_name}`")
# print(f"Vector Search Index mirror of Gold Delta Table: `{gold_chunks_index_name}`")
# print("--")

# dbutils.widgets.text(
#     "embedding_endpoint_name",
#     DATABRICKS_FMAPI_BGE_ENDPOINT,
#     label="Parameter: embedding endpoint",
# )
# embedding_endpoint_name = dbutils.widgets.get("embedding_endpoint_name")

# print(f"Embedding model endpoint: `{embedding_endpoint_name}`")
# print("--")
# dbutils.widgets.text(
#     "chunk_size_tokens", str(CHUNK_SIZE_TOKENS), label="Parameter: chunk size"
# )
# chunk_size_tokens = int(dbutils.widgets.get("chunk_size_tokens"))

# dbutils.widgets.text(
#     "chunk_overlap_tokens", str(CHUNK_OVERLAP_TOKENS), label="Parameter: chunk overlap"
# )
# chunk_overlap_tokens = int(dbutils.widgets.get("chunk_overlap_tokens"))

# COMMAND ----------

# MAGIC %md # Pipeline code

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bronze: Load the files from the UC Volume

# COMMAND ----------

# DBTITLE 1,Recursive PDF Ingestion Workflow
LOADER_DEFAULT_DOC_URI_COL_NAME = "path"
DOC_URI_COL_NAME = "doc_uri"

bronze_df = (
    spark.read.format("binaryFile")
    .option("recursiveFileLookup", "true")
    .option("pathGlobFilter", "*.pdf")
    .load(source_uc_volume)
)

bronze_df = bronze_df.selectExpr(f"* except({LOADER_DEFAULT_DOC_URI_COL_NAME})", f"{LOADER_DEFAULT_DOC_URI_COL_NAME} as {DOC_URI_COL_NAME}")

bronze_df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(bronze_raw_files_table_name)

# reload to get correct lineage in UC
bronze_df = spark.read.table(bronze_raw_files_table_name)

display(bronze_df.selectExpr(f"{DOC_URI_COL_NAME}", "modificationTime", "length"))

if bronze_df.count() == 0:
    url = f"https://{dbutils.notebook.entry_point.getDbutils().notebook().getContext().browserHostName().get()}/explore/data{source_uc_volume}/"
    display(f"`{source_uc_volume}` does not contain any PDF files.  Open the volume and upload at least 1 PDF file: {url}")
    raise Exception(f"`{source_uc_volume}` does not contain any PDF files.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Silver: Parse the PDF files into text
# MAGIC
# MAGIC If you want to change the parsing library or adjust it's settings, modify the contents of the `parse_pdf` UDF.

# COMMAND ----------

# MAGIC %md
# MAGIC # TODO
# MAGIC It appears the below code will require a single user cluster, in order to use a pyspark UDF
# MAGIC
# MAGIC [UDF_PYSPARK_UNSUPPORTED_TYPE] PySpark UDF parse_pdf(content#690)#683 (SQL_ARROW_BATCHED_UDF) is not supported on clusters in Shared access mode. SQLSTATE: 0A000
# MAGIC File <command-2668253031914479>, line 35
# MAGIC      32 df_parsed = bronze_df.withColumn("parsed_output", parse_pdf("content")).drop("content")
# MAGIC      34 # Check and warn on any errors
# MAGIC ---> 35 num_errors = df_parsed.filter(func.col("parsed_output.status") != "SUCCESS").count()
# MAGIC      36 if num_errors > 0:
# MAGIC      37     warning.warn(f"{num_errors} documents had parse errors.  Please review.")
# MAGIC File /databricks/spark/python/pyspark/sql/connect/dataframe.py:257, in DataFrame.count(self)
# MAGIC     256 def count(self) -> int:
# MAGIC --> 257     table, _ = self.agg(F._invoke_function("count", F.lit(1)))._to_table()
# MAGIC     258     return table[0][0].as_py()
# MAGIC File /databricks/spark/python/pyspark/sql/connect/dataframe.py:1824, in DataFrame._to_table(self)
# MAGIC    1822 def _to_table(self) -> Tuple["pa.Table", Optional[StructType]]:
# MAGIC    1823     query = self._plan.to_proto(self._session.client)
# MAGIC -> 1824     table, schema = self._session.client.to_table(query, self._plan.observations)
# MAGIC    1825     assert table is not None
# MAGIC    1826     return (table, schema)
# MAGIC File /databricks/spark/python/pyspark/sql/connect/client/core.py:934, in SparkConnectClient.to_table(self, plan, observations)
# MAGIC     932 req = self._execute_plan_request_with_metadata()
# MAGIC     933 req.plan.CopyFrom(plan)
# MAGIC --> 934 table, schema, _, _, _ = self._execute_and_fetch(req, observations)
# MAGIC     935 assert table is not None
# MAGIC     936 return table, schema
# MAGIC File /databricks/spark/python/pyspark/sql/connect/client/core.py:1525, in SparkConnectClient._execute_and_fetch(self, req, observations, self_destruct)
# MAGIC    1522 schema: Optional[StructType] = None
# MAGIC    1523 properties: Dict[str, Any] = {}
# MAGIC -> 1525 for response in self._execute_and_fetch_as_iterator(req, observations):
# MAGIC    1526     if isinstance(response, StructType):
# MAGIC    1527         schema = response
# MAGIC File /databricks/spark/python/pyspark/sql/connect/client/core.py:1503, in SparkConnectClient._execute_and_fetch_as_iterator(self, req, observations)
# MAGIC    1501                     yield from handle_response(b)
# MAGIC    1502 except Exception as error:
# MAGIC -> 1503     self._handle_error(error)
# MAGIC File /databricks/spark/python/pyspark/sql/connect/client/core.py:1809, in SparkConnectClient._handle_error(self, error)
# MAGIC    1807 self.thread_local.inside_error_handling = True
# MAGIC    1808 if isinstance(error, grpc.RpcError):
# MAGIC -> 1809     self._handle_rpc_error(error)
# MAGIC    1810 elif isinstance(error, ValueError):
# MAGIC    1811     if "Cannot invoke RPC" in str(error) and "closed" in str(error):
# MAGIC File /databricks/spark/python/pyspark/sql/connect/client/core.py:1884, in SparkConnectClient._handle_rpc_error(self, rpc_error)
# MAGIC    1881             info = error_details_pb2.ErrorInfo()
# MAGIC    1882             d.Unpack(info)
# MAGIC -> 1884             raise convert_exception(
# MAGIC    1885                 info,
# MAGIC    1886                 status.message,
# MAGIC    1887                 self._fetch_enriched_error(info),
# MAGIC    1888                 self._display_server_stack_trace(),
# MAGIC    1889             ) from None
# MAGIC    1891     raise SparkConnectGrpcException(status.message) from None
# MAGIC    1892 else:
# MAGIC

# COMMAND ----------

# DBTITLE 1,Optimized PDF Parsing Function
# If using runtime < 14.3, remove `useArrow=True`
# useArrow=True which optimizes performance only works with 14.3+

@func.udf(
    returnType=StructType(
        [
            StructField("number_pages", IntegerType(), nullable=True),
            StructField("text", StringType(), nullable=True),
            StructField("status", StringType(), nullable=False),
        ]
    ),
    # useArrow=True, # set globally
)
def parse_pdf(pdf_raw_bytes):
    try:
        pdf = io.BytesIO(pdf_raw_bytes)
        reader = PdfReader(pdf)
        output_text = ""
        for _, page_content in enumerate(reader.pages):
            output_text += page_content.extract_text() + "\n\n"

        return {
            "number_pages": len(reader.pages),
            "text": output_text,
            "status": "SUCCESS",
        }
    except Exception as e:
        return {"number_pages": None, "text": None, "status": f"ERROR: {e}"}


# Run the parsing
df_parsed = bronze_df.withColumn("parsed_output", parse_pdf("content")).drop("content")

# Check and warn on any errors
num_errors = df_parsed.filter(func.col("parsed_output.status") != "SUCCESS").count()
if num_errors > 0:
    warning.warn(f"{num_errors} documents had parse errors.  Please review.")

df_parsed.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(silver_parsed_files_table_name)

# reload to get correct lineage in UC and to filter out any error rows for the downstream step.
df_parsed = spark.read.table(silver_parsed_files_table_name).filter(
    func.col("parsed_output.status") == "SUCCESS"
)

display(df_parsed)

# COMMAND ----------

# MAGIC %md ## Gold: Chunk the parsed text
# MAGIC
# MAGIC If you change your embedding model, you will need to adjust the tokenizer accordingly.
# MAGIC
# MAGIC If you are using a cluster without internet access, remove the below cell and replace the udf with
# MAGIC
# MAGIC ```
# MAGIC @func.udf(returnType=ArrayType(StringType()), useArrow=True)
# MAGIC def split_char_recursive(content: str) -> List[str]:
# MAGIC     text_splitter = RecursiveCharacterTextSplitter(
# MAGIC         chunk_size=chunk_size, chunk_overlap=chunk_overlap
# MAGIC     )
# MAGIC     chunks = text_splitter.split_text(content)
# MAGIC     return [doc for doc in chunks]
# MAGIC ```

# COMMAND ----------

# MAGIC %pip install --quiet tokenizers torch transformers

# COMMAND ----------

from transformers import AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained('BAAI/bge-large-en-v1.5')

# Test the tokenizer
chunk_example_text = "this is some text in a chunk"
encoded_input = tokenizer(chunk_example_text, padding=True, truncation=True, return_tensors='pt')
print(f"Number of tokens in `{chunk_example_text}`: {len(encoded_input['input_ids'][0])}")

# COMMAND ----------

# DBTITLE 1,Text Chunking UDF Writer
CHUNK_COLUMN_NAME = "chunked_text"
CHUNK_ID_COLUMN_NAME = "chunk_id"

# If using runtime < 14.3, remove `useArrow=True`
# useArrow=True which optimizes performance only works with 14.3+

# TODO: Add error handling
@func.udf(returnType=ArrayType(StringType())
          # useArrow=True, # set globally
          )
def split_char_recursive(content: str) -> List[str]:
    text_splitter = CharacterTextSplitter.from_huggingface_tokenizer(
        tokenizer, chunk_size=chunk_size_tokens, chunk_overlap=chunk_overlap_tokens
    )
    chunks = text_splitter.split_text(content)
    return [doc for doc in chunks]


df_chunked = df_parsed.select(
    "*", func.explode(split_char_recursive("parsed_output.text")).alias(CHUNK_COLUMN_NAME)
).drop(func.col("parsed_output"))
df_chunked = df_chunked.select(
    "*", func.md5(func.col(CHUNK_COLUMN_NAME)).alias(CHUNK_ID_COLUMN_NAME)
)

df_chunked.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(gold_chunks_table_name)
display(df_chunked)

# Enable CDC for Vector Search Delta Sync
spark.sql(f"ALTER TABLE {gold_chunks_table_name} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")

# COMMAND ----------

# MAGIC %md 
# MAGIC ## Embed documents & sync to Vector Search index

# COMMAND ----------

# If index already exists, re-sync
try:
    w.vector_search_indexes.sync_index(index_name=gold_chunks_index_name)
# Otherwise, create new index
except ResourceDoesNotExist as ne_error:
    w.vector_search_indexes.create_index(
        name=gold_chunks_index_name,
        endpoint_name=vector_search_endpoint_name,
        primary_key=CHUNK_ID_COLUMN_NAME,
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            embedding_source_columns=[
                EmbeddingSourceColumn(
                    embedding_model_endpoint_name=embedding_endpoint_name,
                    name=CHUNK_COLUMN_NAME,
                )
            ],
            pipeline_type=PipelineType.TRIGGERED,
            source_table=gold_chunks_table_name,
        ),
    )

# COMMAND ----------

# MAGIC %md
# MAGIC # View index status & output tables
# MAGIC
# MAGIC Your index is now embedding & syncing.  Time taken depends on the number of chunks.  You can view the status and how to query the index at the URL below.

# COMMAND ----------

# DBTITLE 1,Data Source URL Generator
def get_table_url(table_fqdn):
    split = table_fqdn.split(".")
    url = f"https://{dbutils.notebook.entry_point.getDbutils().notebook().getContext().browserHostName().get()}/explore/data/{split[0]}/{split[1]}/{split[2]}"
    return url

print("Vector index:\n")
print(w.vector_search_indexes.get_index(gold_chunks_index_name).status.message)
print("\nOutput tables:\n")
print(f"Bronze Delta Table w/ raw files: {get_table_url(bronze_raw_files_table_name)}")
print(f"Silver Delta Table w/ parsed files: {get_table_url(silver_parsed_files_table_name)}")
print(f"Gold Delta Table w/ chunked files: {get_table_url(gold_chunks_table_name)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Copy paste code for the RAG Chain YAML config

# COMMAND ----------

# DBTITLE 1,Vector Search RAG Configuration
rag_config_yaml = f"""
vector_search_endpoint_name: "{vector_search_endpoint_name}"
vector_search_index: "{gold_chunks_index_name}"
# These must be set to use the Review App to match the columns in your index
vector_search_schema:
  primary_key: {CHUNK_ID_COLUMN_NAME}
  chunk_text: {CHUNK_COLUMN_NAME}
  document_source: {DOC_URI_COL_NAME}
vector_search_parameters:
  k: 3
"""

print(rag_config_yaml)
