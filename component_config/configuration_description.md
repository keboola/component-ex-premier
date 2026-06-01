### Connection

Provide the API base URL (including scheme and port, e.g. `https://your-premier-host:12375`), your PREMIER username and password, and the accounting unit ID (ID-UJ) issued by your PREMIER API service administrator. Use **Test Connection** to verify the credentials before saving.

### Rows

Add one row per object you want to extract and pick the **Object to extract**:

- For **Products** also provide the **Warehouse number** (the PREMIER `sklad` number).
- For **Issued / Received invoices** you can optionally limit the run to a **date range**.

Choose **Incremental load** to fetch only records changed since the last run (recommended) or **Full load** to replace the output table on every run.
