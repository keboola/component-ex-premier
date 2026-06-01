The PREMIER System extractor pulls data from the [PREMIER System](https://www.premier.cz/) ERP and accounting platform through its web API.

It is a row-based extractor — each configuration row extracts one business object into its own table:

- **Customers** and **Suppliers** (the partner register)
- **Issued invoices** and **Received invoices**
- **Products / stock items** (per warehouse)

Authentication uses an HTTP Basic login together with an accounting-unit identifier (ID-UJ) issued by your PREMIER API service administrator. Loads can be incremental — only records changed since the previous run are fetched, using the API's change timestamp persisted in the component state — or a full reload on every run.
