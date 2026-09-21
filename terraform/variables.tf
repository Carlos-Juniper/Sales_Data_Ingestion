variable "project_id" {
  description = "GCP project ID"
  type        = string
  default     = "juniper-crm-498215-p5"
}

variable "region" {
  description = "GCP region for all resources"
  type        = string
  default     = "us-east1"
}

variable "image_tag" {
  description = "Docker image tag to deploy (commit SHA or 'latest')"
  type        = string
  default     = "latest"
}

variable "gcs_raw_bucket" {
  description = "GCS bucket for raw landing (D7)"
  type        = string
  default     = "juniper-ingest-raw"
}

variable "disable_gcs" {
  description = "Set to '1' to disable GCS uploads (useful for testing)"
  type        = string
  default     = "0"
}

variable "cloud_sql_instance" {
  description = "Cloud SQL instance connection name"
  type        = string
  default     = "juniper-crm-498215-p5:us-east1:juniper-postgres-prod"
}

variable "service_account_email" {
  description = "Service account email for Cloud Run jobs"
  type        = string
  default     = "ingestion-connector@juniper-crm-498215-p5.iam.gserviceaccount.com"
}

variable "db_password_secret" {
  description = "Secret Manager secret name holding the DB password"
  type        = string
  default     = "ingest-db-password"
}

variable "db_user" {
  description = "Database user"
  type        = string
  default     = "ingest_app"
}

variable "db_name" {
  description = "Database name"
  type        = string
  default     = "ingestion"
}

variable "scheduler_timezone" {
  description = "Timezone for Cloud Scheduler jobs"
  type        = string
  default     = "America/New_York"
}

# ---------------------------------------------------------------------------
# NPPES flat-file paths (healthcare vertical)
# These must point to files pre-staged on a GCS-mounted volume or similar.
# Cloud Run jobs do not have persistent disk — the operator must mount these
# or pre-bake the files into the image for a given release.
# TODO: implement a pre-pull step that downloads from GCS before running NPPES.
# ---------------------------------------------------------------------------
variable "nppes_main_glob" {
  description = "Glob for the NPPES main npidata_pfile_*.csv"
  type        = string
  default     = "/data/nppes/npidata_pfile_*.csv"
}

variable "nppes_pl_glob" {
  description = "Glob for the NPPES practice location pl_pfile_*.csv"
  type        = string
  default     = "/data/nppes/pl_pfile_*.csv"
}

# ---------------------------------------------------------------------------
# HOA flat-file path
# ---------------------------------------------------------------------------
variable "hoa_data_glob" {
  description = "Glob for the TREC HOA CSV files"
  type        = string
  default     = "/data/hoa/TREC_HOA_Management_Certificates_*.csv"
}

# ---------------------------------------------------------------------------
# Resort flat-file path
# ---------------------------------------------------------------------------
variable "resort_data_glob" {
  description = "Glob for the FL DBPR lodging hrlodge*.csv files"
  type        = string
  default     = "/data/resort/hrlodge*.csv"
}
