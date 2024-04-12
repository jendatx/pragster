# Databricks notebook source
# DBTITLE 1,Databricks RAG Studio Installer
# MAGIC %run ./wheel_installer

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Imports
import os
import mlflow
from databricks import rag_studio

### START: Ignore this code, temporary workarounds given the Private Preview state of the product
from mlflow.utils import databricks_utils as du
os.environ['MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR'] = "false"

def parse_deployment_info(deployment_info):
  browser_url = du.get_browser_hostname()
  message = f"""Deployment of {deployment_info.model_name} version {deployment_info.model_version} initiated.  This can take up to 15 minutes and the Review App & REST API will not work until this deployment finishes. 

  View status: https://{browser_url}/ml/endpoints/{deployment_info.endpoint_name}
  Review App: {deployment_info.rag_app_url}"""
  return message
### END: Ignore this code, temporary workarounds given the Private Preview state of the product

# COMMAND ----------

# DBTITLE 1,Setup
############
# Specify the full path to the chain notebook
############

# Assuming your chain notebook is in the current directory, this helper line grabs the current path, prepending /Workspace/
# Limitation: RAG Studio does not support logging chains stored in Repos
current_path = '/Workspace' + os.path.dirname(dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get())

chain_notebook_file = "1_hello_world_chain"
chain_notebook_path = f"{current_path}/{chain_notebook_file}"

print(f"Saving chain from: {chain_notebook_path}")

# COMMAND ----------

# DBTITLE 1,Log the model
############
# Log the chain to the Notebook's MLflow Experiment inside a Run
# The model is logged to the Notebook's MLflow Experiment as a run
############

logged_chain_info = rag_studio.log_model(code_path=chain_notebook_path)

print(f"MLflow Run: {logged_chain_info.run_id}")
print(f"Model URI: {logged_chain_info.model_uri}")

############
# If you see this error, go to your chain code and comment out all usage of `dbutils`
############
# ValueError: The file specified by 'code_path' uses 'dbutils' command which are not supported in a chain model. To ensure your code functions correctly, remove or comment out usage of 'dbutils' command.

# COMMAND ----------

# DBTITLE 1,Run the logged model locally
############
# You can test the model locally
# This is the same input that the REST API will accept once deployed.
############
example_input = {
    "messages": [
        {
            "role": "user",
            "content": "Howdy!",
        },
        {
            "role": "assistant",
            "content": "Greetings.",
        },
        {
            "role": "user",
            "content": "It's Friday. Again.",
        }
    ]
}

model = mlflow.langchain.load_model(logged_chain_info.model_uri)
model.invoke(example_input)

# COMMAND ----------

############
# Normally, you would now evaluate the chain, but lets skip ahead to deploying the chain so your stakeholders can use it via a chat UI.
############

# COMMAND ----------

############
# To deploy the model, first register the chain from the MLflow Run as a Unity Catalog model.
############

# Assuming logged_chain_info and its model_uri attribute is defined correctly elsewhere

# Your specified Unity Catalog identifiers
uc_catalog = "users"
uc_schema = "jen_darrouzet"
model_name = "hello_world"

# Construct the fully qualified domain name (FQDN) for the model
uc_model_fqdn = f"{uc_catalog}.{uc_schema}.{model_name}"

# Print the FQDN to verify format
print("Attempting to register model with FQDN:", uc_model_fqdn)

# Explicit check for FQDN format, though your error doesn't imply this is the issue
if len(uc_model_fqdn.split('.')) != 3:
    raise ValueError("Model FQDN does not have the correct format 'catalog_name.schema_name.model_name'")

# Set the registry URI to connect with Databricks Unity Catalog
mlflow.set_registry_uri('databricks-uc')

# Register the model in MLflow, using the FQDN and the URI from a logged model
try:
    uc_registered_chain_info = mlflow.register_model(model_uri=logged_chain_info.model_uri, name=uc_model_fqdn)
    print("Model registered successfully.")
except Exception as e:
    print("Failed to register model:", str(e))

# COMMAND ----------

# DBTITLE 1,Deploy the model
############
# Deploy the chain to:
# 1) Review App so you & your stakeholders can chat with the chain & given feedback via a web UI.
# 2) Chain REST API endpoint to call the chain from your front end
# 3) Feedback REST API endpoint to pass feedback back from your front end.
############

deployment_info = rag_studio.deploy_model(uc_model_fqdn, uc_registered_chain_info.version)
print(parse_deployment_info(deployment_info))

# Note: It can take up to 15 minutes to deploy - we are working to reduce this time to seconds.

# COMMAND ----------

# DBTITLE 1,View deployments
############
# If you lost the deployment information captured above, you can find it using list_deployments()
############
deployments = rag_studio.list_deployments()
for deployment in deployments:
  if deployment.model_name == uc_model_fqdn and deployment.model_version==uc_registered_chain_info.version:
    print(parse_deployment_info(deployment))
