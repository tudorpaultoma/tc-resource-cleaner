# Tencent Cloud Resource Cleaner

Automatically deletes CLB (Cloud Load Balancer) and CBS (Cloud Block Storage) resources based on TTL tags and project assignments.

## Features

- **Tag-based deletion** - Uses TaggerTTL, TaggerCreated, TaggerDelete, TaggerProject, and TaggerLinkedCVM tags
- **Multi-region support** - Processes all Tencent Cloud regions or specific regions
- **Dry-run mode** - Test without actual deletion
- **Selective resource processing** - Enable/disable CLB and CBS independently
- **SCF deployment** - Runs as serverless function with timer trigger

## Deployment

### 1. Build Package

```bash
bash deploy.sh
```

Creates `scf-resource-cleaner.zip` (43MB) ready for SCF upload.

### 2. Create SCF Function

- **Runtime**: Python 3.9
- **Handler**: `index.main_handler`
- **Upload**: `scf-resource-cleaner.zip`
- **Memory**: 512MB (recommended)
- **Timeout**: 900s (15 minutes)

### 3. Configure Environment Variables

**Required:**
```
ENABLE_CLB=true
ENABLE_CBS=true
```

**Optional:**
```
DEFAULT_TTL_DAYS=7
DRY_RUN=false
REGIONS=ap-singapore,ap-hongkong
```

- `DEFAULT_TTL_DAYS` - Default TTL if TaggerTTL tag missing (default: 7)
- `DRY_RUN` - Set to `true` for testing without deletion (default: false)
- `REGIONS` - Comma-separated regions, leave empty for all regions

### 4. Set IAM Policy

Attach CAM policy with required permissions:

```json
{
  "version": "2.0",
  "statement": [{
    "effect": "allow",
    "action": [
      "name/clb:DescribeLoadBalancers",
      "name/clb:DeleteLoadBalancer",
      "name/cvm:DescribeDisks",
      "name/cvm:TerminateDisks",
      "name/tag:DescribeResourcesByTags"
    ],
    "resource": "*"
  }]
}
```

**Note**: CBS APIs use `name/cvm:` prefix, not `cbs:`.

### 5. Configure Trigger

**Timer (Cron):**
```
0 2 * * *
```
Runs daily at 2 AM UTC.

## Tag Structure

Resources must have these tags:

| Tag | Description | Example |
|-----|-------------|---------|
| `TaggerTTL` | Time-to-live in days | `7` |
| `TaggerCreated` | Creation date | `2026-02-24` |
| `TaggerDelete` | Explicit delete flag | `YES` or `NO` |
| `TaggerProject` | Project assignment | `project-name` or `n/a` |
| `TaggerLinkedCVM` | CBS only: attached to CVM | `YES` or `NO` |

## Deletion Strategy

### CLB (Cloud Load Balancer)

**Delete if:**
1. TTL expired + `TaggerDelete=YES`
2. TTL expired + `TaggerDelete=NO` + `TaggerProject=n/a`

**Skip if:**
- TTL not expired
- `TaggerDelete=NO` + `TaggerProject` has value
- Missing required tags

### CBS (Cloud Block Storage)

**Delete ONLY if:**
- TTL expired + `TaggerLinkedCVM=NO` + `TaggerProject=n/a`

**Never delete if:**
- `TaggerLinkedCVM=YES` (protected - strategy pending)
- `TaggerProject` has value (protected)
- TTL not expired
- Missing required tags

## Testing

Start with dry-run mode:

```bash
# Set in SCF environment
DRY_RUN=true
ENABLE_CLB=true
ENABLE_CBS=true
```

Check logs for `[DRY RUN]` messages showing what would be deleted.

## Local Testing

```bash
export DEFAULT_TTL_DAYS=7
export DRY_RUN=true
export ENABLE_CLB=true
export ENABLE_CBS=true
export TENCENTCLOUD_SECRET_ID=your_id
export TENCENTCLOUD_SECRET_KEY=your_key

python3 index.py
```

## Output

Execution summary includes:

```
Execution Summary:
CLB:
  Total scanned: 10
  Pending deletion: 3
  Successfully deleted: 3
  Skipped: 7
  Errors: 0
CBS:
  Total scanned: 25
  Pending deletion: 5
  Successfully deleted: 5
  Skipped: 20
  Errors: 0
```

## Supported Regions

All Tencent Cloud regions:
- Asia Pacific: bangkok, beijing, chengdu, chongqing, guangzhou, hongkong, jakarta, mumbai, nanjing, seoul, shanghai, singapore, tokyo
- Europe: frankfurt, moscow
- North America: ashburn, siliconvalley, toronto
- South America: saopaulo

## Safety Features

- **Dry-run mode** - Test before production
- **Conservative CBS deletion** - Never deletes attached disks (`TaggerLinkedCVM=YES`)
- **Tag validation** - Skips resources with missing/invalid tags
- **Error handling** - Continues processing if individual resources fail
- **Detailed logging** - Full audit trail of all decisions

## License

See [LICENSE](LICENSE) file.
