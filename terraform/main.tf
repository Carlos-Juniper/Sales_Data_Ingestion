# Juniper ArcGIS Ingest — Cloud Run Jobs + Cloud Scheduler
#
# One-time prerequisites (operator must run before first `terraform init`):
#   gsutil mb -l us-east1 -p juniper-crm-498215-p5 gs://juniper-tf-state
#   gsutil versioning set on gs://juniper-tf-state
#   gcloud artifacts repositories create ingestion \
#     --repository-format=docker \
#     --location=us-east1 \
#     --project=juniper-crm-498215-p5
#
# Apply:
#   terraform init
#   terraform apply -var="image_tag=<COMMIT_SHA>"

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  backend "gcs" {
    bucket = "juniper-tf-state"
    prefix = "arcgis-ingest"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ---------------------------------------------------------------------------
# API enablement
# ---------------------------------------------------------------------------

resource "google_project_service" "run" {
  project            = var.project_id
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "scheduler" {
  project            = var.project_id
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "artifactregistry" {
  project            = var.project_id
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "secretmanager" {
  project            = var.project_id
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  image = "us-east1-docker.pkg.dev/${var.project_id}/ingestion/connector:${var.image_tag}"

  # Database URL is composed at startup from the secret password.
  # The scripts pull the password from Secret Manager themselves, so we only
  # need to pass the non-secret parts as plain env vars.
  common_env = [
    {
      name  = "GCS_RAW_BUCKET"
      value = var.gcs_raw_bucket
    },
    {
      name  = "DISABLE_GCS"
      value = var.disable_gcs
    },
    {
      name  = "DB_USER"
      value = var.db_user
    },
    {
      name  = "DB_NAME"
      value = var.db_name
    },
    {
      name  = "DB_PORT"
      value = "5432"
    },
    # Cloud Run built-in connector exposes the socket at /cloudsql/<instance>.
    # The shell scripts compose DATABASE_URL using DB_HOST=localhost and the
    # TCP port surfaced by the built-in connector (same host as the job).
    {
      name  = "DB_HOST"
      value = "localhost"
    },
  ]
}

# ---------------------------------------------------------------------------
# Shared Cloud Run Job template helper
# The four verticals share identical structure; only VERTICAL and extra env
# vars differ.  Terraform does not support reusable modules inline, so we
# define each job explicitly but reference the same image and locals.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Healthcare Cloud Run Job
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "healthcare" {
  name     = "ingest-healthcare"
  location = var.region
  project  = var.project_id

  depends_on = [google_project_service.run]

  template {
    template {
      service_account = var.service_account_email

      # Cloud SQL built-in connector — no Auth Proxy sidecar needed.
      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [var.cloud_sql_instance]
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      timeout = "86400s"  # 24-hour max; enrichment cache warm-up can be slow

      containers {
        image = local.image

        command = ["/bin/bash"]
        args    = ["/app/scripts/run_healthcare.sh"]

        env {
          name  = "VERTICAL"
          value = "healthcare"
        }

        # Non-secret env vars
        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.value.name
            value = env.value.value
          }
        }

        # NPPES flat-file globs — operator must pre-stage files or mount a volume.
        # TODO: implement a GCS pre-pull step for NPPES bulk data before enabling.
        env {
          name  = "NPPES_MAIN_GLOB"
          value = var.nppes_main_glob
        }
        env {
          name  = "NPPES_PL_GLOB"
          value = var.nppes_pl_glob
        }

        # DB password from Secret Manager — injected as env var for gcloud CLI usage
        # inside the scripts. The script calls `gcloud secrets versions access` and
        # composes DATABASE_URL itself, so no Secret Manager SDK is needed.
        # The SA already holds secretmanager.secretAccessor on this secret.
        env {
          name = "DB_PASSWORD_SECRET"
          value_source {
            secret_key_ref {
              secret  = var.db_password_secret
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      # Allow Cloud Build to bump the image tag without Terraform re-deploying.
      template[0].template[0].containers[0].image,
    ]
  }
}

# ---------------------------------------------------------------------------
# Deathcare Cloud Run Job
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "deathcare" {
  name     = "ingest-deathcare"
  location = var.region
  project  = var.project_id

  depends_on = [google_project_service.run]

  template {
    template {
      service_account = var.service_account_email

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [var.cloud_sql_instance]
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      timeout = "86400s"

      containers {
        image = local.image

        command = ["/bin/bash"]
        args    = ["/app/scripts/run_deathcare.sh"]

        env {
          name  = "VERTICAL"
          value = "deathcare"
        }

        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.value.name
            value = env.value.value
          }
        }

        env {
          name = "DB_PASSWORD_SECRET"
          value_source {
            secret_key_ref {
              secret  = var.db_password_secret
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

# ---------------------------------------------------------------------------
# HOA Cloud Run Job
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "hoa" {
  name     = "ingest-hoa"
  location = var.region
  project  = var.project_id

  depends_on = [google_project_service.run]

  template {
    template {
      service_account = var.service_account_email

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [var.cloud_sql_instance]
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      timeout = "86400s"

      containers {
        image = local.image

        command = ["/bin/bash"]
        args    = ["/app/scripts/run_hoa.sh"]

        env {
          name  = "VERTICAL"
          value = "hoa"
        }

        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.value.name
            value = env.value.value
          }
        }

        env {
          name  = "HOA_DATA_GLOB"
          value = var.hoa_data_glob
        }

        env {
          name = "DB_PASSWORD_SECRET"
          value_source {
            secret_key_ref {
              secret  = var.db_password_secret
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

# ---------------------------------------------------------------------------
# Resort Cloud Run Job
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "resort" {
  name     = "ingest-resort"
  location = var.region
  project  = var.project_id

  depends_on = [google_project_service.run]

  template {
    template {
      service_account = var.service_account_email

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [var.cloud_sql_instance]
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      timeout = "86400s"

      containers {
        image = local.image

        command = ["/bin/bash"]
        args    = ["/app/scripts/run_resort.sh"]

        env {
          name  = "VERTICAL"
          value = "resort"
        }

        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.value.name
            value = env.value.value
          }
        }

        env {
          name  = "RESORT_DATA_GLOB"
          value = var.resort_data_glob
        }

        env {
          name = "DB_PASSWORD_SECRET"
          value_source {
            secret_key_ref {
              secret  = var.db_password_secret
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

# ---------------------------------------------------------------------------
# IAM — grant the runtime/invoking SA roles/run.invoker on each job.
#
# Cloud Scheduler calls each job's run URL using var.service_account_email's
# OAuth token (see http_target.oauth_token below), so that same SA must hold
# run.invoker on the job it is scheduling. Declared here instead of a manual
# `gcloud run jobs add-iam-policy-binding` so it's applied automatically on
# every `terraform apply`.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job_iam_member" "healthcare_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.healthcare.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_email}"
}

resource "google_cloud_run_v2_job_iam_member" "deathcare_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.deathcare.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_email}"
}

resource "google_cloud_run_v2_job_iam_member" "hoa_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.hoa.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_email}"
}

resource "google_cloud_run_v2_job_iam_member" "resort_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.resort.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_email}"
}

# ---------------------------------------------------------------------------
# Cloud Scheduler jobs
#
# Cadence rationale (from D9 + §7.5):
#   healthcare — weekly (NPPES is weekly, the most frequent source)
#                "0 2 * * 0"  = Sunday 02:00
#   deathcare  — monthly (IRS BMF is monthly)
#                "0 3 1 * *"  = 1st of month 03:00
#   hoa        — quarterly (TREC HOA publishes quarterly)
#                "0 4 1 1,4,7,10 *"  = 1st of Jan/Apr/Jul/Oct 04:00
#   resort     — monthly (FL DBPR refreshes weekly but lodging list is stable)
#                "0 5 1 * *"  = 1st of month 05:00
#
# The scheduler invokes the Cloud Run job via HTTP POST to the job's run URL.
# The invoking SA's run.invoker grant is declared above.
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "healthcare" {
  name      = "ingest-healthcare-weekly"
  project   = var.project_id
  region    = var.region
  schedule  = "0 2 * * 0"
  time_zone = var.scheduler_timezone

  depends_on = [
    google_project_service.scheduler,
    google_cloud_run_v2_job.healthcare,
  ]

  http_target {
    http_method = "POST"
    uri = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/ingest-healthcare:run"

    oauth_token {
      service_account_email = var.service_account_email
    }
  }
}

resource "google_cloud_scheduler_job" "deathcare" {
  name      = "ingest-deathcare-monthly"
  project   = var.project_id
  region    = var.region
  schedule  = "0 3 1 * *"
  time_zone = var.scheduler_timezone

  depends_on = [
    google_project_service.scheduler,
    google_cloud_run_v2_job.deathcare,
  ]

  http_target {
    http_method = "POST"
    uri = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/ingest-deathcare:run"

    oauth_token {
      service_account_email = var.service_account_email
    }
  }
}

resource "google_cloud_scheduler_job" "hoa" {
  name      = "ingest-hoa-quarterly"
  project   = var.project_id
  region    = var.region
  # Quarterly: 1st of Jan, Apr, Jul, Oct at 04:00
  schedule  = "0 4 1 1,4,7,10 *"
  time_zone = var.scheduler_timezone

  depends_on = [
    google_project_service.scheduler,
    google_cloud_run_v2_job.hoa,
  ]

  http_target {
    http_method = "POST"
    uri = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/ingest-hoa:run"

    oauth_token {
      service_account_email = var.service_account_email
    }
  }
}

resource "google_cloud_scheduler_job" "resort" {
  name      = "ingest-resort-monthly"
  project   = var.project_id
  region    = var.region
  schedule  = "0 5 1 * *"
  time_zone = var.scheduler_timezone

  depends_on = [
    google_project_service.scheduler,
    google_cloud_run_v2_job.resort,
  ]

  http_target {
    http_method = "POST"
    uri = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/ingest-resort:run"

    oauth_token {
      service_account_email = var.service_account_email
    }
  }
}

# ---------------------------------------------------------------------------
# Parks / Municipal vertical
# ---------------------------------------------------------------------------
# Needs no API keys or pre-staged flat files: every source is a public ArcGIS
# endpoint (Census TIGERweb, USGS PAD-US, PASDA, TPWD, FDEP, NCDPR, SCPRT), so
# this job carries only the common env plus the DB password.
resource "google_cloud_run_v2_job" "parks" {
  name     = "ingest-parks"
  location = var.region
  project  = var.project_id

  depends_on = [google_project_service.run]

  template {
    template {
      service_account = var.service_account_email

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [var.cloud_sql_instance]
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      timeout = "86400s"

      containers {
        image = local.image

        command = ["/bin/bash"]
        args    = ["/app/scripts/run_parks.sh"]

        env {
          name  = "VERTICAL"
          value = "parks"
        }

        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.value.name
            value = env.value.value
          }
        }

        env {
          name = "DB_PASSWORD_SECRET"
          value_source {
            secret_key_ref {
              secret  = var.db_password_secret
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

resource "google_cloud_run_v2_job_iam_member" "parks_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.parks.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_account_email}"
}

# Quarterly, not monthly. Every upstream source in this vertical revises on an
# annual cycle at best: TIGER publishes once a year, PAD-US roughly annually, and
# the state park layers change when a park is acquired or reclassified. A monthly
# schedule would spend a 24-hour job slot re-downloading ~72k identical polygons.
# Quarterly still catches a new TIGER vintage within one cycle of release.
resource "google_cloud_scheduler_job" "parks" {
  name      = "ingest-parks-quarterly"
  project   = var.project_id
  region    = var.region
  schedule  = "0 6 1 1,4,7,10 *"
  time_zone = var.scheduler_timezone

  depends_on = [
    google_project_service.scheduler,
    google_cloud_run_v2_job.parks,
  ]

  http_target {
    http_method = "POST"
    uri = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/ingest-parks:run"

    oauth_token {
      service_account_email = var.service_account_email
    }
  }
}
