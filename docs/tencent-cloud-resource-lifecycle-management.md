# Tencent Cloud Resource Lifecycle Management

## Automated Tagging, Scheduling & Cleanup System

---

## 1. Executive Summary

This document describes an automated resource lifecycle management system deployed on Tencent Cloud. The system consists of three serverless functions (SCF) that work together to:

1. **Tag** every newly created cloud resource with standardized metadata
2. **Schedule** CVM instances to stop/start based on office hours
3. **Delete** expired and orphaned resources based on TTL (Time-To-Live) policies

The goal is to **eliminate cloud waste**, **enforce resource ownership**, and **ensure temporary resources are cleaned up automatically** — without manual intervention.

---

## 2. System Architecture

### 2.1 Components

| Component | SCF Function | Trigger | Schedule |
|-----------|-------------|---------|----------|
| **Resource Tagger** | `tc-tagger-function` | COS event (CloudAudit log delivery) | Real-time (on resource creation) |
| **CVM Scheduler** | `tc-cvm-tag-shutdown` | Timer | Every 30 minutes (outside working hours in EU) |
| **Resource Cleaner** | `tc-resource-cleaner` | Timer | Daily at 8 PM |

### 2.2 Flow Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                        RESOURCE CREATION                         │
│  User creates CVM, CLB, CBS, EIP, ENI, HAVIP, Snapshot, NAT, or AS group │
└──────────────────────┬───────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                    ① TAGGER FUNCTION                             │
│  CloudAudit captures event → COS log → SCF processes            │
│  Applies: Owner, Created date, TTL, Project, CanDelete, etc.    │
└──────────────────────┬───────────────────────────────────────────┘
                       │
           ┌───────────┴───────────┐
           ▼                       ▼
┌─────────────────────┐  ┌─────────────────────────────────────────┐
│ ② CVM SCHEDULER     │  │ ③ RESOURCE CLEANER                     │
│ Every 30 min:       │  │ Daily at 8 PM:                          │
│ • Stop outside hrs  │  │ • Delete expired CLB, CBS, EIP, ENI,   │
│ • Start during hrs  │  │   HAVIP, Snapshot, NAT, AS based on    │
│ • Terminate expired │  │   TTL + project + state                 │
└─────────────────────┘  └─────────────────────────────────────────┘
```

### 2.3 Covered Services

| Service | Full Name | Tagged | Scheduled | Cleaned |
|---------|-----------|:------:|:---------:|:-------:|
| **CVM** | Cloud Virtual Machine | ✅ | ✅ | ✅ (via scheduler) |
| **CDH** | Cloud Dedicated Host | ✅ | — | — |
| **CLB** | Cloud Load Balancer | ✅ | — | ✅ |
| **CBS** | Cloud Block Storage | ✅ | — | ✅ |
| **EIP** | Elastic IP | ✅ | — | ✅ |
| **ENI** | Elastic Network Interface | ✅ | — | ✅ |
| **HAVIP** | High Availability Virtual IP | ✅ | — | ✅ |
| **Snapshot** | CBS Snapshot | ✅ | — | ✅ |
| **NAT** | NAT Gateway (Public) | ✅ | — | ✅ |
| **AS** | Auto Scaling (Groups + Launch Configs) | ✅ | — | ✅ |

### 2.4 Region Coverage

All functions operate across 18 Tencent Cloud regions:

ap-bangkok, ap-beijing, ap-chengdu, ap-chongqing, ap-guangzhou, ap-hongkong, ap-jakarta, ap-nanjing, ap-seoul, ap-shanghai, ap-shanghai-fsi, ap-shenzhen-fsi, ap-singapore, ap-tokyo, eu-frankfurt, na-ashburn, na-siliconvalley, sa-saopaulo

Not all regions covered because the Cloud Audit API is not up to date with all regions.
---

## 3. Component 1: Resource Tagger

### 3.1 Purpose

Automatically applies a standardized set of tags to every newly created cloud resource. Tags are applied in near real-time by monitoring CloudAudit event logs delivered to COS.

### 3.2 How It Works

1. **CloudAudit** captures resource creation events (e.g., `RunInstances`, `CreateLoadBalancer`) and writes log files to a COS bucket.
2. A **COS trigger** invokes the tagger function whenever a new log file is delivered.
3. The function parses the log, identifies the created resource, determines the owner from the `userIdentity` field, and calls the **Tag API** to apply standardized tags.

### 3.3 Monitored Events

| Service | Events |
|---------|--------|
| CVM | `RunInstances`, `AllocateHosts` |
| CLB | `CreateLoadBalancer` |
| CBS | `CreateCbsStorages`, `CreateDisks`, `AttachDisks` |
| EIP | `AllocateAddresses`, `TransformAddress` |
| ENI | `CreateNetworkInterface` |
| HAVIP | `CreateHaVip` |
| Snapshot | `CreateSnapshot` |
| NAT Gateway | `CreateNatGateway` |
| Auto Scaling | `CreateAutoScalingGroup`, `CreateLaunchConfiguration` |

### 3.4 Tags Applied

#### Common Tags (all resources)

| Tag Key | Default Value | Description |
|---------|---------------|-------------|
| `TaggerOwner` | _(auto-detected)_ | Email, username, or account ID of the creator |
| `TaggerCreated` | _(current date)_ | Creation date in `YYYY-MM-DD` format |
| `TaggerTTL` | `3` | Time-to-live in days before cleanup actions begin |
| `TaggerCanDelete` | `YES` | Whether the resource is eligible for auto-deletion |
| `TaggerProject` | `n/a` | Project assignment — `n/a` means unassigned |

#### CVM / CDH Additional Tags

| Tag Key | Default Value | Description |
|---------|---------------|-------------|
| `TaggerAutoOff` | `YES` | Auto-stop outside office hours |
| `TaggerAutoStart` | `NO` | Auto-start during office hours (opt-in) |

#### CBS Additional Tags

| Tag Key | Default Value | Description |
|---------|---------------|-------------|
| `TaggerLinkedCVM` | `YES` or `NO` | Whether the disk is attached to a CVM |
| `TaggerUsage` | `SYSTEM` or `DATA` | Disk type |

> **CBS Project Inheritance:** When a disk is attached to a CVM, the tagger copies the CVM's `TaggerProject` value to the disk. Unattached disks default to `n/a`.

#### EIP / ENI Additional Tags

| Tag Key | Default Value | Description |
|---------|---------------|-------------|
| `TaggerLinkedResource` | Instance ID or `NONE` | The resource the EIP/ENI is bound to |

#### EIP Additional Tags

| Tag Key | Default Value | Description |
|---------|---------------|-------------|
| `TaggerType` | _(varies)_ | EIP type (e.g., `AnycastEIP`, `HighQualityEIP`) |

#### HAVIP Additional Tags

| Tag Key | Default Value | Description |
|---------|---------------|-------------|
| `TaggerSubnet` | _(subnet ID)_ | Subnet where the HAVIP resides |
| `TaggerVpc` | _(VPC ID)_ | VPC where the HAVIP resides |

### 3.5 Owner Detection Priority

The tagger identifies the resource creator using this priority order:

1. `userEmail` from the CloudAudit event
2. `userName` or `displayName`
3. `accountId` (prefixed with `account:`)
4. `uin` (prefixed with `uin:`)
5. Falls back to `unknown`

---

## 4. Component 2: CVM Scheduler

### 4.1 Purpose

Automatically stops CVM instances outside of EU working hours to save costs, and optionally starts them when office hours begin. Also terminates CVM instances that have exceeded their TTL.

### 4.2 Office Hours Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| Start time | `09:00` | Beginning of office hours |
| End time | `18:00` | End of office hours |
| Work days | Monday–Friday | Days considered "office hours" |
| Timezone offset | `+1` (EU / CET) | UTC offset for time calculations |

### 4.3 Stop/Start Logic

| TaggerAutoOff | TaggerAutoStart | Outside Office Hours | During Office Hours |
|:---:|:---:|:---|:---|
| `YES` | `YES` | **Stopped** automatically | **Started** automatically |
| `YES` | `NO` or missing | **Stopped** automatically | Must be started manually |
| Missing | Any | No action | No action |

**Key details:**
- Stop uses `STOP_CHARGING` mode — billing stops when the instance is shut down
- Graceful shutdown is used (`ForceStop=False`)
- Instances are stopped/started every 30 minutes on schedule

### 4.4 CVM TTL Termination

Independent of office hours, the scheduler also evaluates TTL expiration:

- **Condition:** `Current Date - TaggerCreated ≥ TaggerTTL`
- **Action:** Instance is **terminated** (permanently deleted)
- **Prepaid instances:** If termination fails (e.g., prepaid), auto-renewal is disabled instead

> ⚠️ **CVM termination is irreversible.** Always test with `DRY_RUN=true` first.

---

## 5. Component 3: Resource Cleaner

### 5.1 Purpose

Deletes expired non-CVM resources (CLB, CBS, EIP, ENI, HAVIP, Snapshot, NAT Gateway, Auto Scaling) based on TTL tags, project assignment, and resource state.

### 5.2 General Deletion Criteria

For a resource to be deleted, **ALL** of the following must be true:

1. **TTL expired:** `Current Date - TaggerCreated ≥ TaggerTTL`
2. **Eligible for deletion** (one of):
   - `TaggerCanDelete = YES`, OR
   - `TaggerCanDelete = NO` but `TaggerProject = n/a`, OR
   - No `TaggerCanDelete` tag and `TaggerProject` is `n/a` or missing

**Resources are NEVER deleted if:**
- TTL has not expired
- `TaggerCanDelete = NO` **and** `TaggerProject` has a real project name (both conditions required)
- `TaggerTTL` or `TaggerCreated` tags are missing

> ⚠️ `TaggerCanDelete = NO` alone does **not** protect a resource. A real `TaggerProject` (not `n/a`) is mandatory to prevent deletion.

### 5.3 Service-Specific Rules

#### CLB (Cloud Load Balancer)

Follows the general deletion criteria above. No additional state checks.

#### CBS (Cloud Block Storage)

**Most conservative strategy.** Deleted ONLY if:
- TTL expired AND
- `TaggerLinkedCVM = NO` (disk is not attached to any CVM) AND
- `TaggerProject = n/a`

**Never deleted if:**
- Disk is attached to a CVM (`TaggerLinkedCVM = YES`)
- Assigned to a real project

#### EIP (Elastic IP)

**Pre-check:** Skipped immediately if:
- EIP is bound to an instance (status `BIND` or `BIND_ENI`)
- `TaggerLinkedResource` ≠ `NONE`
- Status is not `UNBIND`

Only **unbound** EIPs with expired TTL are released.

#### ENI (Elastic Network Interface)

**Pre-check:** Skipped immediately if:
- Primary ENI (always deleted with its parent CVM)
- Currently attached to a CVM instance
- State is not `AVAILABLE`
- `TaggerLinkedResource` ≠ `NONE`

Only **detached, secondary** ENIs with expired TTL are deleted.

#### HAVIP (High Availability Virtual IP)

Follows the general deletion criteria. No additional state checks.

#### Snapshot (CBS Snapshot)

**Pre-check:** Skipped immediately if:
- Snapshot state is not `NORMAL`

Only snapshots in **NORMAL** state with expired TTL are deleted.

#### NAT Gateway (Public)

Follows the general deletion criteria. No additional state checks.

#### Auto Scaling (Groups + Launch Configurations)

**Scaling Groups — Pre-check:** Skipped immediately if:
- Group has running instances (`InstanceCount > 0`)

Only **empty** scaling groups (0 instances) with expired TTL are deleted.

**Launch Configurations — Pre-check:** Skipped immediately if:
- Referenced by an active scaling group

Only **unreferenced** launch configurations with expired TTL are deleted.

---

## 6. Protection Mechanisms

The system has multiple layers of protection to prevent accidental deletion of important resources:

### 6.1 Tag-Based Protection

| Method | How | Effect |
|--------|-----|--------|
| **Assign a project** | Set `TaggerProject` to any value other than `n/a` | Resource is never auto-deleted |
| **Set CanDelete to NO** | Set `TaggerCanDelete = NO` with a real project | Resource is never auto-deleted |
| **Increase TTL** | Set `TaggerTTL` to a high number (e.g., `365`) | Delays cleanup by that many days |
| **Remove TTL tag** | Delete the `TaggerTTL` tag entirely | Resource is ignored by cleaners |

### 6.2 State-Based Protection (automatic)

| Resource | Protected When |
|----------|---------------|
| CBS Disk | Attached to a CVM (`TaggerLinkedCVM = YES`) — follows CVM's lifecycle |
| EIP | Bound to an instance (status `BIND` or `BIND_ENI`) — follows CVM's lifecycle |
| ENI | Primary ENI, or attached to a CVM — follows CVM's lifecycle |
| CVM | Not applicable — only TTL termination applies |

> **Important:** Bound/attached resources (disks, EIPs, ENIs) are always safe while attached, but they inherit the CVM's lifecycle. If the CVM is terminated, these resources become unattached and will be subject to cleanup. Unattached/unused resources have **zero tolerance** — they are deleted as soon as their TTL expires.

### 6.3 Dry-Run Mode

All three functions support a `DRY_RUN=true` environment variable. When enabled:
- All scanning and evaluation runs normally
- **No resources are actually modified, stopped, or deleted**
- Actions that _would_ be taken are logged

**Recommendation:** Always deploy with `DRY_RUN=true` first and review logs before switching to production mode.

---

## 7. Default Behavior Summary

When a resource is created with **no manual tag changes**, the following happens:

| Resource | Default Tags | What Happens |
|----------|-------------|--------------|
| **CVM** | TTL=3, AutoOff=YES, AutoStart=NO, CanDelete=YES, Project=n/a | Stopped outside office hours after creation. **Terminated after 3 days.** |
| **CLB** | TTL=3, CanDelete=YES, Project=n/a | **Deleted after 3 days.** |
| **CBS (attached)** | TTL=3, LinkedCVM=YES, Project=_(inherited from CVM)_ | Protected while attached — follows CVM's lifecycle. Deleted if detached + project is n/a + TTL expired. |
| **CBS (standalone)** | TTL=3, LinkedCVM=NO, Project=n/a | **Deleted after 3 days.** Zero tolerance for unattached disks. |
| **EIP (bound)** | TTL=3, LinkedResource=_(instance ID)_ | Protected while bound — follows CVM's lifecycle. Released if unbound + TTL expired. |
| **EIP (unbound)** | TTL=3, LinkedResource=NONE | **Released after 3 days.** Zero tolerance for unbound EIPs. |
| **ENI (primary)** | TTL=3 | Always protected (deleted with CVM). |
| **ENI (secondary, attached)** | TTL=3 | Protected while attached — follows CVM's lifecycle. |
| **ENI (secondary, detached)** | TTL=3, LinkedResource=NONE | **Deleted after 3 days.** Zero tolerance for detached ENIs. |
| **HAVIP** | TTL=3, CanDelete=YES, Project=n/a | **Deleted after 3 days.** |
| **Snapshot** | TTL=3, CanDelete=YES, Project=n/a | **Deleted after 3 days** (only if state is NORMAL). |
| **NAT Gateway** | TTL=3, CanDelete=YES, Project=n/a | **Deleted after 3 days.** |
| **AS Group** | TTL=3, CanDelete=YES, Project=n/a | **Deleted after 3 days** (only if 0 running instances). |
| **AS Launch Config** | TTL=3, CanDelete=YES, Project=n/a | **Deleted after 3 days** (only if not referenced by a group). |

---

## 8. How to Keep Your Resources

If you want to prevent auto-deletion, do **any one** of the following:

### Option A: Assign a Project (Recommended)
```
TaggerProject = my-project-name
```
Resources with a real project name are never auto-deleted. However, they **will be reported upon weekly** for review.

### Option B: Increase TTL
```
TaggerTTL = 30      (or 90, 365, etc.)
```
The resource will not be cleaned up until this many days after creation.

### Option C: Set CanDelete + Project (mandatory combination)
```
TaggerCanDelete = NO
TaggerProject = my-project-name    ← mandatory if you want to retain it
```
Double protection — explicit opt-out of deletion with project assignment. **Setting `TaggerCanDelete=NO` alone is not enough** — you must also assign a real `TaggerProject` (not `n/a`), otherwise the resource will still be deleted.

### Option D: Remove the TTL Tag
Delete `TaggerTTL` entirely. The cleanup functions ignore resources without this tag.

---

## 9. Deployment Summary

### 9.1 Function Specifications

| Function | Runtime | Memory | Timeout | Handler |
|----------|---------|--------|---------|---------|
| Tagger | Python 3.9 | 512 MB | 150s | `index.main_handler` |
| CVM Scheduler | Python 3.9 | 512 MB | 300s | `index.main_handler` |
| Resource Cleaner | Python 3.9 | 256 MB | 300s | `index.main_handler` |

### 9.2 Trigger Configuration

| Function | Trigger Type | Configuration |
|----------|-------------|---------------|
| Tagger | COS Event | `cos:ObjectCreated:*` on audit bucket with prefix `cloudaudit/` |
| CVM Scheduler | Timer | Every 30 minutes (outside working hours in EU): `0 */30 * * * * *` |
| Resource Cleaner | Timer | Daily at 8 PM: `0 20 * * *` |

### 9.3 IAM Permissions

Each function requires a CAM execution role with specific permissions. Refer to the `iam-policy.json` file in each repository for the exact policy.

---

## 10. Frequently Asked Questions

**Q: I just created a CVM and it already has tags. Is that normal?**
A: Yes. The tagger function applies tags within seconds of resource creation via CloudAudit event monitoring.

**Q: My CVM stopped overnight. Is something wrong?**
A: No. If it has `TaggerAutoOff=YES`, it is automatically stopped outside office hours. Set `TaggerAutoOff=NO` or `TaggerAutoStart=YES` to have it restart automatically.

**Q: How do I prevent my resource from being deleted?**
A: Set `TaggerProject` to your project name (mandatory). Or increase `TaggerTTL` to a high value. If you set `TaggerCanDelete=NO`, you **must** also assign a real `TaggerProject`. See Section 8 for all options. Note: resources assigned to a project are not auto-deleted but will be reported upon weekly.

**Q: What happens if I remove all Tagger tags?**
A: The resource becomes invisible to all three functions. It will not be stopped, started, or deleted automatically.

**Q: Is there a way to test without risk?**
A: Yes. All functions support `DRY_RUN=true`. Set this environment variable to see what actions would be taken without executing them.

**Q: Are prepaid/reserved instances affected?**
A: The CVM scheduler attempts termination on expired prepaid instances. If the API rejects it, it disables auto-renewal instead. The instance continues running until the prepaid period ends.

**Q: Which services are NOT covered?**
A: Currently, CDB (databases), VPN, and other services are not covered by this system. CVM, CDH, CLB, CBS, EIP, ENI, HAVIP, Snapshot, NAT Gateway, and Auto Scaling are managed.

---

## 11. Repository Links

| Component | Repository |
|-----------|-----------|
| Resource Tagger | https://github.com/tudorpaultoma/tc-tagger-function |
| CVM Scheduler | https://github.com/tudorpaultoma/tc-cvm-tag-shutdown |
| Resource Cleaner | https://github.com/tudorpaultoma/tc-resource-cleaner |
