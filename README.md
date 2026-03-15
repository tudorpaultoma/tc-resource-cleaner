# Tencent Cloud Resource Cleaner

![Version](https://img.shields.io/badge/version-2.0.0-blue)

Automatically deletes expired cloud resources based on TTL tags and project assignments. Designed to run as a Tencent Cloud SCF (Serverless Cloud Function).

## Supported Services

| Service | Description | Delete API |
|---------|-------------|------------|
| **CLB** | Cloud Load Balancer | `DeleteLoadBalancer` |
| **CBS** | Cloud Block Storage | `TerminateDisks` |
| **EIP** | Elastic IP | `ReleaseAddresses` |
| **ENI** | Elastic Network Interface | `DeleteNetworkInterface` |
| **HAVIP** | High Availability Virtual IP | `DeleteHaVip` |

## Project Structure

```
tc-resource-cleaner/
├── index.py              # SCF handler & orchestrator
├── services/
│   ├── __init__.py       # Package exports
│   ├── base.py           # Shared base class (client factory, tag helpers, TTL logic)
│   ├── clb.py            # CLB cleanup
│   ├── cbs.py            # CBS cleanup
│   ├── eip.py            # EIP cleanup
│   ├── eni.py            # ENI cleanup
│   └── havip.py          # HAVIP cleanup
├── iam-policy.json       # CAM policy template
├── deploy.sh             # Build deployment package
├── requirements.txt      # Python dependencies
└── README.md
```

## Features

- **Tag-based deletion** — Uses TaggerTTL, TaggerCreated, TaggerCanDelete, TaggerProject, TaggerLinkedCVM, and TaggerLinkedResource tags
- **Multi-region support** — Processes 18 Tencent Cloud regions (or specific regions)
- **Dry-run mode** — Test without actual deletion
- **Selective processing** — Enable/disable each service independently
- **Pagination** — Handles accounts with large numbers of resources
- **Modular design** — Each service in its own module, shared base class

## Deployment

### 1. Build Package

```bash
bash deploy.sh
```

Creates `scf-resource-cleaner.zip` ready for SCF upload.

### 2. Create SCF Function

- **Runtime**: Python 3.9
- **Handler**: `index.main_handler`
- **Upload**: `scf-resource-cleaner.zip`
- **Memory**: 256MB
- **Timeout**: 300s (5 minutes)
- **Execution Role**: Attach a CAM role with the policy from `iam-policy.json`

### 3. Configure Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_CLB` | `true` | Enable CLB cleanup |
| `ENABLE_CBS` | `true` | Enable CBS cleanup |
| `ENABLE_EIP` | `true` | Enable EIP cleanup |
| `ENABLE_ENI` | `true` | Enable ENI cleanup |
| `ENABLE_HAVIP` | `true` | Enable HAVIP cleanup |
| `DEFAULT_TTL_DAYS` | `7` | Default TTL if tag value is invalid |
| `DRY_RUN` | `false` | Set `true` for testing without deletion |
| `REGIONS` | _(all)_ | Comma-separated regions (e.g. `ap-tokyo,eu-frankfurt`) |

### 4. Set IAM Policy

Attach the CAM policy in `iam-policy.json` to the SCF execution role:

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
      "name/cvm:DescribeAddresses",
      "name/cvm:ReleaseAddresses",
      "name/cvm:DescribeNetworkInterfaces",
      "name/cvm:DeleteNetworkInterface",
      "name/cvm:DescribeHaVips",
      "name/cvm:DeleteHaVip"
    ],
    "resource": "*"
  }]
}
```

> **Note:** CBS, EIP, ENI, and HAVIP actions all use the `cvm` CAM namespace, even though they have separate API endpoints.

### 5. Configure Trigger

**Timer (Cron):**
```
0 2 * * *
```
Runs daily at 2 AM UTC.

## Tag Structure

Resources must have these tags to be evaluated:

| Tag | Required | Description | Values |
|-----|----------|-------------|--------|
| `TaggerTTL` | Yes | Time-to-live in days | `7`, `30`, etc. |
| `TaggerCreated` | Yes | Creation date | `2026-02-24` |
| `TaggerCanDelete` | No | Explicit delete flag | `YES` / `NO` |
| `TaggerProject` | No | Project assignment | project name or `n/a` |
| `TaggerLinkedCVM` | CBS only | Attached to CVM | `YES` / `NO` |
| `TaggerLinkedResource` | EIP/ENI | Bound instance ID or NONE | `ins-abc123` / `NONE` |

## Deletion Strategy

### CLB (Cloud Load Balancer)

**Delete if TTL expired AND:**
1. `TaggerCanDelete=YES`, OR
2. `TaggerCanDelete=NO` + `TaggerProject=n/a`, OR
3. No `TaggerCanDelete` tag + `TaggerProject` is `n/a` or missing

**Skip if:**
- TTL not expired
- `TaggerCanDelete=NO` + `TaggerProject` has a real value
- Missing `TaggerTTL` or `TaggerCreated` tags

### CBS (Cloud Block Storage)

**Delete ONLY if TTL expired AND:**
- `TaggerLinkedCVM=NO` + `TaggerProject=n/a`

**Never delete if:**
- `TaggerLinkedCVM=YES`
- `TaggerProject` has a real value
- TTL not expired
- Missing required tags

### EIP (Elastic IP)

**Skip immediately if:**
- EIP is bound to an instance (status `BIND`/`BIND_ENI` or `TaggerLinkedResource` ≠ `NONE`)
- EIP status is not `UNBIND` (only unbound EIPs can be released)

**Delete if TTL expired AND status is UNBIND AND:**
1. `TaggerCanDelete=YES`, OR
2. `TaggerCanDelete=NO` + `TaggerProject=n/a`, OR
3. No `TaggerCanDelete` tag + `TaggerProject` is `n/a` or missing

### ENI (Elastic Network Interface)

**Skip immediately if:**
- Primary ENI (deleted with CVM)
- Attached to a CVM instance
- State is not `AVAILABLE`
- `TaggerLinkedResource` ≠ `NONE`

**Delete if TTL expired AND detached AND:**
1. `TaggerCanDelete=YES`, OR
2. `TaggerCanDelete=NO` + `TaggerProject=n/a`, OR
3. No `TaggerCanDelete` tag + `TaggerProject` is `n/a` or missing

### HAVIP (High Availability Virtual IP)

**Delete if TTL expired AND:**
1. `TaggerCanDelete=YES`, OR
2. `TaggerCanDelete=NO` + `TaggerProject=n/a`, OR
3. No `TaggerCanDelete` tag + `TaggerProject` is `n/a` or missing

**Skip if:**
- TTL not expired
- `TaggerCanDelete=NO` + `TaggerProject` has a real value
- Missing `TaggerTTL` or `TaggerCreated` tags

## Local Testing

```bash
export DRY_RUN=true
export ENABLE_CLB=true
export ENABLE_CBS=true
export ENABLE_EIP=true
export ENABLE_ENI=true
export ENABLE_HAVIP=true
export TENCENTCLOUD_SECRETID=your_id
export TENCENTCLOUD_SECRETKEY=your_key

python3 index.py
```

## Supported Regions

ap-bangkok, ap-beijing, ap-chengdu, ap-chongqing, ap-guangzhou, ap-hongkong, ap-jakarta, ap-nanjing, ap-seoul, ap-shanghai, ap-shanghai-fsi, ap-shenzhen-fsi, ap-singapore, ap-tokyo, eu-frankfurt, na-ashburn, na-siliconvalley, sa-saopaulo

## Changelog

### v2.0.0
- **Breaking**: Refactored monolithic `index.py` into modular `services/` package
- Added ENI (Elastic Network Interface) cleanup
- Added HAVIP (High Availability Virtual IP) cleanup
- Shared base class with client factory, tag helpers, TTL logic
- Added `ENABLE_ENI` and `ENABLE_HAVIP` environment variables
- Updated IAM policy with ENI/HAVIP permissions

### v1.0.0
- Initial release with CLB, CBS, EIP cleanup
- Tag-based TTL deletion strategy
- Multi-region support, dry-run mode

## License

See [LICENSE](LICENSE) file.
