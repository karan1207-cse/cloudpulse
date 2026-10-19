# All tables use on-demand billing and AWS-owned encryption keys (no extra
# cost). Schemas match src/cloudpulse/dynamodb_storage.py and
# src/cloudpulse/dynamodb_incident_storage.py.

# Health-check history: one partition per service, sorted by time.
#   PK service (S), SK sk = "<checked_at>#<id>" (S), TTL expires_at.
resource "aws_dynamodb_table" "health_checks" {
  name                        = local.health_table_name
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "service"
  range_key                   = "sk"
  deletion_protection_enabled = var.enable_deletion_protection

  attribute {
    name = "service"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}

# Incidents: PK id. GSIs answer "incidents for a service" and "recent
# OPEN/RECOVERED incidents" without scanning the table.
resource "aws_dynamodb_table" "incidents" {
  name                        = local.incidents_table_name
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "id"
  deletion_protection_enabled = var.enable_deletion_protection

  attribute {
    name = "id"
    type = "S"
  }

  attribute {
    name = "service_name"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = "service-created-index"
    hash_key        = "service_name"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "status-created-index"
    hash_key        = "status"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = var.enable_point_in_time_recovery
  }

  server_side_encryption {
    enabled = true
  }
}

# Monitoring state: one item per service (failure counter, active incident,
# optimistic-concurrency version). Kept in its own table so state records can
# never be counted as incidents.
resource "aws_dynamodb_table" "monitor_state" {
  name                        = local.state_table_name
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "service"
  deletion_protection_enabled = var.enable_deletion_protection

  attribute {
    name = "service"
    type = "S"
  }

  point_in_time_recovery {
    enabled = var.enable_point_in_time_recovery
  }

  server_side_encryption {
    enabled = true
  }
}
