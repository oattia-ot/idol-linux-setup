# Configuring NiFi Integration in OpenText Find (Linux)

This guide explains how to configure **OpenText Find** to connect to **Apache NiFi** so that users can apply policies (actions) to their search results.

**Environment in this guide:**
- **Find URL**: `http://localhost:8080/config`
- **NiFi URL**: `https://localhost:8443/nifi/#/login`

---

## 1. Prerequisites

Before you start, ensure the following are in place:

- **Java JRE 11 (64-bit)** is installed on Linux.
- **Apache NiFi** is installed and running on `localhost:8443`.
- **Find** is installed and running on `localhost:8080`.
- A NiFi dataflow with a **`NiFiHandleAciRequest`** processor (or a suitable Input Port) is configured to receive requests from Find.
- You have administrative access to both Find and NiFi.

---

## 2. Step-by-Step Find Configuration

### 2.1. Open the Find Settings Page
1. Open your browser and navigate to:  
   `http://localhost:8080/config`
2. Log in with an account that has administrative privileges.

### 2.2. Locate the NiFi Settings Section
Scroll down to the **NiFi** area on the Settings page.

### 2.3. Enter NiFi Connection Details
Fill in the fields with the following values:

| Field          | Value                      | Explanation                                      |
|----------------|----------------------------|--------------------------------------------------|
| **Enable NiFi**| Toggle **ON**              | Activates the integration.                       |
| **Protocol**   | `https`                    | Your NiFi URL uses HTTPS.                        |
| **Host**       | `localhost`                | NiFi runs on the same machine as Find.           |
| **Port**       | `8443`                     | The port from your NiFi URL.                     |

### 2.4. Save Changes
Click the **Save Changes** button at the bottom of the page.

---

## 3. Handle the SSL Certificate Issue (Critical)

By default, NiFi uses a **self-signed SSL certificate**. When Find (running on Java) tries to connect to `https://localhost:8443`, it will reject the connection with an `SSLHandshakeException`. You must resolve this.

Choose **one** of the following options:

### Option A: Disable SSL Certificate Validation (Quick Test Only)
This option is suitable for **testing** or **development** environments.

1. Stop the Find service or process.
2. Locate Find's startup script (e.g., `find.cmd`) or the service parameters.
3. Add the following JVM argument to the startup command: