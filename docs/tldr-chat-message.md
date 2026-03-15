## ⚡ Tencent Cloud Auto-Tagging & Cleanup — TL;DR

We have an automated system that **tags every new cloud resource at creation** and **cleans up expired resources** on a schedule. Here's how it works:

**1. Auto-Tagging (runs on every resource creation)**
When you create a CVM, CLB, CBS disk, EIP, ENI, or HAVIP — a serverless function automatically tags it with: owner, creation date, TTL (default: 3 days), project, and deletion eligibility.

**2. CVM Auto Stop/Start (runs every 30 min outside working hours in EU)**
CVMs tagged with `TaggerAutoOff=YES` are **automatically stopped outside office hours** (configurable). If `TaggerAutoStart=YES`, they startup when office hours begin. 
CVMs that exceed their TTL are **terminated**.

**3. Other Resource Cleanup (runs daily at 8 PM)**
CLB, CBS, EIP, ENI, and HAVIP resources are **deleted when their TTL expires**, provided they are not assigned to a project and not actively in use; bound disks/EIPs/ENIs are always safe, but they have CVM's lifecycle.

**⚠️ What you need to know:**
- Every resource you create gets a **3-day TTL by default** — after 3 days it will be shut down or deleted
- To keep a resource: set `TaggerTTL` to a higher number, or set `TaggerCanDelete=NO` + assign a real `TaggerProject` (mandatory if you want to retain it)
- Resources assigned to a project (`TaggerProject` ≠ `n/a`) are **not auto-deleted** ; but they will be reported upon weekly.
- Attached disks, bound EIPs, and primary ENIs are **always protected** regardless of tags. Unattached/unused resources have 0 tolerance.

**Covered services:** CVM, CDH, CLB, CBS, EIP, ENI, HAVIP — across 18 regions.
