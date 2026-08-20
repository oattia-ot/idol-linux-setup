Setting up Statistics for Find, QMS, and Content with OpenText IDOL Statistics Server
This guide explains how to configure the Statistics Server to collect and aggregate analytics from Find, QMS, and Content IDOL components. The Statistics Server receives event data (queries, clicks, page views, abandonment, etc.) and maintains time‑based counters and top‑N lists.

Note: NiFi is not a direct source of statistical events for the Statistics Server. NiFi is a data flow tool that may ingest or enrich data, but it does not generate query/click events. If you need to monitor NiFi itself, use its own administrative interface. This guide focuses on the IDOL components that generate user‑interaction events.

1. Prerequisites
The Statistics Server component is installed (typically bundled with IDOL Server or QMS).

Content, QMS, and Find are already installed and running.

Each component is configured to send event XML to the Statistics Server’s EventPort (default 19871).

You have a valid license for the Statistics Server (or it is included in the IDOL license).

2. Overview of Configuration Files
The Statistics Server uses a single configuration file: statsserver.cfg.
This file defines:

Server settings (ports, threads, logging).

The IDOLStatistics section – which identifies the event sources and the field that indicates the source.

A Statistics index – a numbered list of all statistical metrics to track.

A separate section for each metric – containing the criteria (operation, field filters, time period, etc.).

You have two starting configurations:

Existing configuration (for Content and QMS) – contains metrics like ContentQueryHourlyCount and QmsRulesDailyTop100000.

Find‑required configuration (statsserver-required-config.cfg) – contains metrics for Find paging, abandonment, and clickthrough.

You need to merge these two files into a single statsserver.cfg.

3. Step‑by‑Step Setup
3.1. Merge the Configuration Files
Locate the existing statsserver.cfg (the one you already use for Content and QMS).

Open the Find‑required file (statsserver-required-config.cfg) and copy all the [Find...] sections (the metric definitions).

Paste those sections into your existing statsserver.cfg (append them at the end).

Update the [IDOLStatistics] and [Statistics] sections:

Set Number=3 (for Content, QMS, and Find).

Set EventField to the field name that each event carries to identify its source.
We recommend using a common field like idolname.

For Content, ensure each event contains idolname=content.

For QMS, ensure idolname=qms.

For Find, ensure idolname=Find (case‑sensitive).
(If your events already use a different field, adjust EventField accordingly.)

In the [Statistics] index, append the Find metric names (e.g., FindPaging10Minute) as new numbered entries. You can renumber the entire list sequentially.

Verify each metric section has the correct IDOLName parameter:

IDOLName=content for Content‑related metrics.

IDOLName=qms for QMS metrics.

IDOLName=Find for Find metrics.

3.2. Ensure Event Sending is Enabled
Each component must send events to the Statistics Server’s EventPort.
This is typically configured in the component’s own .cfg file:

For Content and QMS, set EventHost and EventPort to point to the Statistics Server.

For Find, enable statistics by setting the Statistics Server URL in the Find administration page (or in find-config.json).

3.3. Enable ActionEvent
In the [Server] section of statsserver.cfg, make sure ActionEvent=TRUE is set (this is already present in both files).

3.4. Set the Correct Offset and Period
The Find metrics include an Offset date (e.g., 2016/04/11 00:00:00). This defines the start of the cumulative window. You may keep it as‑is or adjust to a date when your Find deployment went live.

3.5. Restart the Statistics Server
After saving the merged statsserver.cfg, restart the Statistics Server:

As a service (systemd or equivalent): restart via services.msc.

As a standalone: stop and restart statsserver.exe.

4. Sample Merged Configuration (Excerpt)
Below is a partial view of the merged [IDOLStatistics] and [Statistics] sections.
(Full merged file is provided later.)

ini
[IDOLStatistics]
EventField=idolname          # Common field identifying the source
Number=3                     # three sources

[Statistics]
0=ContentQueryTenMinuteCount
1=ContentQueryHourlyCount
...
12=QmsRulesWeeklyTop100000
13=FindPaging10Minute
14=FindPaging1Hour
...
42=FindClickthroughFullPreviewLifetime

[FindPaging10Minute]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=600
5. Complete Merged statsserver.cfg
You can find the full merged configuration below. Replace your existing statsserver.cfg with this content.

ini
[License] < "idol.common.cfg" [License]

[Service]
ServicePort=19872
Access-Control-Allow-Origin=http://localhost:19870
XSLTemplates=TRUE

[Server]
Port=19870
EventPort=19871
EventClients=localhost
Threads=4
MaxInputString=64000
XSLTemplates=TRUE
EventThreads=8
ActionEvent=TRUE

//--------------------------- Authorization Roles ----------------------------//
[AuthorizationRoles]
0=AdminRole
1=QueryRole

[AdminRole] < "idol.common.cfg" [AdminRole]
[QueryRole] < "idol.common.cfg" [QueryRole]
//----------------------------------------------------------------------------//

[Paths]
History=./history
Main=./main
TemplateDirectory=./templates

//--------------------------- Combined IDOLStatistics -------------------------//
[IDOLStatistics]
EventField=idolname
Number=3

//--------------------------- Statistics Index -------------------------------//
[Statistics]
0=ContentQueryTenMinuteCount
1=ContentQueryHourlyCount
2=ContentQueryDailyCount
3=ContentQueryHourlyTop10Terms
4=ContentQueryDailyTop10Terms
5=ContentQueryWeeklyTop10Terms
6=ContentZeroHitTenMinuteCount
7=ContentZeroHitQueryHourlyCount
8=ContentZeroHitQueryDailyCount
9=ContentZeroHitQueryWeeklyTop10Terms
10=QmsRulesHourlyTop100000
11=QmsRulesDailyTop100000
12=QmsRulesWeeklyTop100000
13=FindPaging10Minute
14=FindPaging1Hour
15=FindPaging1Day
16=FindPaging1Week
17=FindPagingLifetime
18=FindAbandonmentPreview10Minute
19=FindAbandonmentPreview1Hour
20=FindAbandonmentPreview1Day
21=FindAbandonmentPreview1Week
22=FindAbandonmentPreviewLifetime
23=FindAbandonmentOriginal10Minute
24=FindAbandonmentOriginal1Hour
25=FindAbandonmentOriginal1Day
26=FindAbandonmentOriginal1Week
27=FindAbandonmentOriginalLifetime
28=FindClickthroughPreview10Minute
29=FindClickthroughPreview1Hour
30=FindClickthroughPreview1Day
31=FindClickthroughPreview1Week
32=FindClickthroughPreviewLifetime
33=FindClickthroughOriginal10Minute
34=FindClickthroughOriginal1Hour
35=FindClickthroughOriginal1Day
36=FindClickthroughOriginal1Week
37=FindClickthroughOriginalLifetime
38=FindClickthroughFullPreview10Minute
39=FindClickthroughFullPreview1Hour
40=FindClickthroughFullPreview1Day
41=FindClickthroughFullPreview1Week
42=FindClickthroughFullPreviewLifetime

//--------------------- Existing Statistics (Content & QMS) ------------------//
[ContentQueryTenMinuteCount]
IDOLName=content
Operation=count
Field=queryinfo/action
AEqualStat=action,query
Period=600

[ContentQueryHourlyCount]
IDOLName=content
Operation=count
Field=queryinfo/action
AEqualStat=action,query
Period=3600

[ContentQueryDailyCount]
IDOLName=content
Operation=count
Field=queryinfo/action
AEqualStat=action,query
Period=86400

[ContentQueryHourlyTop10Terms]
IDOLName=content
Operation=topn,10
Field=queryinfo/terms/term
AEqualStat=action,query
Period=3600

[ContentQueryDailyTop10Terms]
IDOLName=content
Operation=topn,10
Field=queryinfo/terms/term
AEqualStat=action,query
Period=86400

[ContentQueryWeeklyTop10Terms]
IDOLName=content
Operation=topn,10
Field=queryinfo/terms/term
AEqualStat=action,query
Period=604800
Offset=2014/11/03 00:00:00

[ContentZeroHitTenMinuteCount]
IDOLName=content
Operation=count
Field=queryinfo/numhits
NEqualStat=queryinfo/numhits,0
AEqualStat=action,query
Period=600

[ContentZeroHitQueryHourlyCount]
IDOLName=content
Operation=count
Field=queryinfo/numhits
NEqualStat=queryinfo/numhits,0
AEqualStat=action,query
Period=3600

[ContentZeroHitQueryDailyCount]
IDOLName=content
Operation=count
Field=queryinfo/numhits
NEqualStat=queryinfo/numhits,0
AEqualStat=action,query
Period=86400

[ContentZeroHitQueryWeeklyTop10Terms]
IDOLName=content
Operation=topn,10
Field=queryinfo/terms/term
NEqualStat=queryinfo/numhits,0
AEqualStat=action,query
Period=604800
Offset=2014/11/03 00:00:00

[QmsRulesHourlyTop100000]
IDOLName=qms
Operation=topn,100000
Field=queryinfo/results/reference
Period=3600
Offset=2014/11/03 00:00:00

[QmsRulesDailyTop100000]
IDOLName=qms
Operation=topn,100000
Field=queryinfo/results/reference
Period=86400
Offset=2014/11/03 00:00:00

[QmsRulesWeeklyTop100000]
IDOLName=qms
Operation=topn,100000
Field=queryinfo/results/reference
Period=604800
Offset=2014/11/03 00:00:00

//--------------------- Find Statistics (from required config) --------------//
[FindPaging10Minute]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=600

[FindPaging1Hour]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=3600

[FindPaging1Day]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=86400

[FindPaging1Week]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=604800

[FindPagingLifetime]
IDOLName=Find
Operation=CumulativeTopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=600

[FindAbandonmentPreview10Minute]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=600

[FindAbandonmentPreview1Hour]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=3600

[FindAbandonmentPreview1Day]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=86400

[FindAbandonmentPreview1Week]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=604800

[FindAbandonmentPreviewLifetime]
IDOLName=Find
Operation=CumulativeCount
AEqualStat=find-event/type,abandonment,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=600

[FindAbandonmentOriginal10Minute]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=600

[FindAbandonmentOriginal1Hour]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=3600

[FindAbandonmentOriginal1Day]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=86400

[FindAbandonmentOriginal1Week]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=604800

[FindAbandonmentOriginalLifetime]
IDOLName=Find
Operation=CumulativeCount
AEqualStat=find-event/type,abandonment,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughPreview10Minute]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughPreview1Hour]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=3600

[FindClickthroughPreview1Day]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=86400

[FindClickthroughPreview1Week]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=604800

[FindClickthroughPreviewLifetime]
IDOLName=Find
Operation=CumulativeTopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughOriginal10Minute]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughOriginal1Hour]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=3600

[FindClickthroughOriginal1Day]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=86400

[FindClickthroughOriginal1Week]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=604800

[FindClickthroughOriginalLifetime]
IDOLName=Find
Operation=CumulativeTopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughFullPreview10Minute]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,full_preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughFullPreview1Hour]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,full_preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=3600

[FindClickthroughFullPreview1Day]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,full_preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=86400

[FindClickthroughFullPreview1Week]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,full_preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=604800

[FindClickthroughFullPreviewLifetime]
IDOLName=Find
Operation=CumulativeTopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,full_preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

//--------------------------- Logging ----------------------------------------//
[Logging] < "idol.common.cfg" [Logging]

0=ApplicationLogStream
1=QueryLogStream
2=EventLogStream

[ApplicationLogStream] < "idol.common.cfg" [ApplicationLogStream]
[QueryLogStream] < "idol.common.cfg" [QueryLogStream]
[EventLogStream] < "idol.common.cfg" [EventLogStream]
6. Verifying the Setup
After restarting the Statistics Server, you can check that events are being received and statistics are being calculated:

Check the EventLog – look for lines indicating events are being processed.

Query the Statistics Server via HTTP:

http://<StatsServerHost>:<ServicePort>/action=GetStatus – shows basic server health.

http://<StatsServerHost>:<ServicePort>/action=GetStatistics – lists all tracked metrics and their current values (use ?StatisticName=FindPaging10Minute to filter).

From Find, go to the statistics dashboard (if available) to see the aggregated data.

7. Troubleshooting
Issue	Likely Cause	Solution
No statistics appear	Events are not reaching the Statistics Server	Check that EventPort is open and that components are sending to the correct host/port. Verify EventClients includes the component IP.
Find metrics are empty	The EventField does not match the field in Find events	Ensure EventField matches the XML field that contains Find (or content, qms). If Find uses find-event/event, set EventField=find-event/event and adjust IDOLName accordingly.
Content/QMS stats missing after merge	The IDOLName in the metric sections does not match the IDOLName in the event	Verify each metric has IDOLName equal to the value sent in the event’s identifier field.
Server fails to start	Duplicate section names or syntax errors	Check the configuration for duplicate [Statistics] indices or malformed lines.
8. Additional Notes
NiFi is not a direct event producer for the Statistics Server. If you want to track NiFi‑related metrics, consider using NiFi’s own reporting tasks or integrating with external monitoring tools.

The Offset parameter defines when the cumulative statistics begin. Adjust it to your deployment’s go‑live date if needed.

You can add more custom statistics by appending entries to the [Statistics] list and creating corresponding sections.

For further details, refer to the official IDOL QMS Administration Guide (Statistics Server chapter).

generate a downloaded md file link
The user wants a downloadable markdown file link. I need to provide a link to a markdown file that contains the setup instructions. Since I don't have a hosted file, I can suggest they create the file themselves or I can provide a link to a GitHub gist or similar. Alternatively, I can generate the markdown content and tell them to save it as a .md file. The user might expect a direct download link, but I can't host files. I'll provide the markdown content and instruct them to save it.

I don't have the ability to host or generate downloadable file links directly. However, I can provide you with the complete Markdown content so you can save it as a .md file on your own machine.

How to create the downloadable file
Copy the entire Markdown content below.

Open a text editor (e.g., Notepad, VS Code).

Paste the content.

Save the file as statistics-server-setup.md.

Markdown content to save
markdown
# Setting up Statistics for Find, QMS, and Content with OpenText IDOL Statistics Server

This guide explains how to configure the **Statistics Server** to collect and aggregate analytics from **Find**, **QMS**, and **Content** IDOL components. The Statistics Server receives event data (queries, clicks, page views, abandonment, etc.) and maintains time‑based counters and top‑N lists.

> **Note**: NiFi is not a direct source of statistical events for the Statistics Server. NiFi is a data flow tool that may ingest or enrich data, but it does not generate query/click events. If you need to monitor NiFi itself, use its own administrative interface. This guide focuses on the IDOL components that generate user‑interaction events.

---

## 1. Prerequisites

- The Statistics Server component is installed (typically bundled with IDOL Server or QMS).
- **Content**, **QMS**, and **Find** are already installed and running.
- Each component is configured to **send event XML** to the Statistics Server's `EventPort` (default 19871).
- You have a valid license for the Statistics Server (or it is included in the IDOL license).

---

## 2. Overview of Configuration Files

The Statistics Server uses a single configuration file: `statsserver.cfg`.  
This file defines:

- Server settings (ports, threads, logging).
- The **IDOLStatistics** section – which identifies the event sources and the field that indicates the source.
- A **Statistics** index – a numbered list of all statistical metrics to track.
- A separate section for **each metric** – containing the criteria (operation, field filters, time period, etc.).

You have two starting configurations:

- **Existing configuration** (for Content and QMS) – contains metrics like `ContentQueryHourlyCount` and `QmsRulesDailyTop100000`.
- **Find‑required configuration** (`statsserver-required-config.cfg`) – contains metrics for Find paging, abandonment, and clickthrough.

You need to **merge** these two files into a single `statsserver.cfg`.

---

## 3. Step‑by‑Step Setup

### 3.1. Merge the Configuration Files

1. **Locate** the existing `statsserver.cfg` (the one you already use for Content and QMS).
2. **Open** the Find‑required file (`statsserver-required-config.cfg`) and copy **all** the `[Find...]` sections (the metric definitions).
3. **Paste** those sections into your existing `statsserver.cfg` (append them at the end).
4. **Update** the `[IDOLStatistics]` and `[Statistics]` sections:

   - Set `Number=3` (for Content, QMS, and Find).
   - Set `EventField` to the field name that each event carries to identify its source.  
     *We recommend using a common field like `idolname`.*  
     - For Content, ensure each event contains `idolname=content`.  
     - For QMS, ensure `idolname=qms`.  
     - For Find, ensure `idolname=Find` (case‑sensitive).  
     *(If your events already use a different field, adjust `EventField` accordingly.)*
   - In the `[Statistics]` index, append the Find metric names (e.g., `FindPaging10Minute`) as new numbered entries. You can renumber the entire list sequentially.

5. **Verify** each metric section has the correct `IDOLName` parameter:
   - `IDOLName=content` for Content‑related metrics.
   - `IDOLName=qms` for QMS metrics.
   - `IDOLName=Find` for Find metrics.

### 3.2. Ensure Event Sending is Enabled

Each component must send events to the Statistics Server's `EventPort`.  
This is typically configured in the component's own `.cfg` file:

- For **Content** and **QMS**, set `EventHost` and `EventPort` to point to the Statistics Server.
- For **Find**, enable statistics by setting the Statistics Server URL in the Find administration page (or in `find-config.json`).

### 3.3. Enable `ActionEvent`

In the `[Server]` section of `statsserver.cfg`, make sure `ActionEvent=TRUE` is set (this is already present in both files).

### 3.4. Set the Correct Offset and Period

The Find metrics include an `Offset` date (e.g., `2016/04/11 00:00:00`). This defines the start of the cumulative window. You may keep it as‑is or adjust to a date when your Find deployment went live.

### 3.5. Restart the Statistics Server

After saving the merged `statsserver.cfg`, restart the Statistics Server:

- As a service (systemd or equivalent): restart via `services.msc`.
- As a standalone: stop and restart `statsserver.exe`.

---

## 4. Sample Merged Configuration (Excerpt)

Below is a **partial** view of the merged `[IDOLStatistics]` and `[Statistics]` sections.  
(Full merged file is provided later.)

```ini
[IDOLStatistics]
EventField=idolname          # Common field identifying the source
Number=3                     # three sources

[Statistics]
0=ContentQueryTenMinuteCount
1=ContentQueryHourlyCount
...
12=QmsRulesWeeklyTop100000
13=FindPaging10Minute
14=FindPaging1Hour
...
42=FindClickthroughFullPreviewLifetime

[FindPaging10Minute]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=600
5. Complete Merged statsserver.cfg
You can find the full merged configuration below. Replace your existing statsserver.cfg with this content.

ini
[License] < "idol.common.cfg" [License]

[Service]
ServicePort=19872
Access-Control-Allow-Origin=http://localhost:19870
XSLTemplates=TRUE

[Server]
Port=19870
EventPort=19871
EventClients=localhost
Threads=4
MaxInputString=64000
XSLTemplates=TRUE
EventThreads=8
ActionEvent=TRUE

//--------------------------- Authorization Roles ----------------------------//
[AuthorizationRoles]
0=AdminRole
1=QueryRole

[AdminRole] < "idol.common.cfg" [AdminRole]
[QueryRole] < "idol.common.cfg" [QueryRole]
//----------------------------------------------------------------------------//

[Paths]
History=./history
Main=./main
TemplateDirectory=./templates

//--------------------------- Combined IDOLStatistics -------------------------//
[IDOLStatistics]
EventField=idolname
Number=3

//--------------------------- Statistics Index -------------------------------//
[Statistics]
0=ContentQueryTenMinuteCount
1=ContentQueryHourlyCount
2=ContentQueryDailyCount
3=ContentQueryHourlyTop10Terms
4=ContentQueryDailyTop10Terms
5=ContentQueryWeeklyTop10Terms
6=ContentZeroHitTenMinuteCount
7=ContentZeroHitQueryHourlyCount
8=ContentZeroHitQueryDailyCount
9=ContentZeroHitQueryWeeklyTop10Terms
10=QmsRulesHourlyTop100000
11=QmsRulesDailyTop100000
12=QmsRulesWeeklyTop100000
13=FindPaging10Minute
14=FindPaging1Hour
15=FindPaging1Day
16=FindPaging1Week
17=FindPagingLifetime
18=FindAbandonmentPreview10Minute
19=FindAbandonmentPreview1Hour
20=FindAbandonmentPreview1Day
21=FindAbandonmentPreview1Week
22=FindAbandonmentPreviewLifetime
23=FindAbandonmentOriginal10Minute
24=FindAbandonmentOriginal1Hour
25=FindAbandonmentOriginal1Day
26=FindAbandonmentOriginal1Week
27=FindAbandonmentOriginalLifetime
28=FindClickthroughPreview10Minute
29=FindClickthroughPreview1Hour
30=FindClickthroughPreview1Day
31=FindClickthroughPreview1Week
32=FindClickthroughPreviewLifetime
33=FindClickthroughOriginal10Minute
34=FindClickthroughOriginal1Hour
35=FindClickthroughOriginal1Day
36=FindClickthroughOriginal1Week
37=FindClickthroughOriginalLifetime
38=FindClickthroughFullPreview10Minute
39=FindClickthroughFullPreview1Hour
40=FindClickthroughFullPreview1Day
41=FindClickthroughFullPreview1Week
42=FindClickthroughFullPreviewLifetime

//--------------------- Existing Statistics (Content & QMS) ------------------//
[ContentQueryTenMinuteCount]
IDOLName=content
Operation=count
Field=queryinfo/action
AEqualStat=action,query
Period=600

[ContentQueryHourlyCount]
IDOLName=content
Operation=count
Field=queryinfo/action
AEqualStat=action,query
Period=3600

[ContentQueryDailyCount]
IDOLName=content
Operation=count
Field=queryinfo/action
AEqualStat=action,query
Period=86400

[ContentQueryHourlyTop10Terms]
IDOLName=content
Operation=topn,10
Field=queryinfo/terms/term
AEqualStat=action,query
Period=3600

[ContentQueryDailyTop10Terms]
IDOLName=content
Operation=topn,10
Field=queryinfo/terms/term
AEqualStat=action,query
Period=86400

[ContentQueryWeeklyTop10Terms]
IDOLName=content
Operation=topn,10
Field=queryinfo/terms/term
AEqualStat=action,query
Period=604800
Offset=2014/11/03 00:00:00

[ContentZeroHitTenMinuteCount]
IDOLName=content
Operation=count
Field=queryinfo/numhits
NEqualStat=queryinfo/numhits,0
AEqualStat=action,query
Period=600

[ContentZeroHitQueryHourlyCount]
IDOLName=content
Operation=count
Field=queryinfo/numhits
NEqualStat=queryinfo/numhits,0
AEqualStat=action,query
Period=3600

[ContentZeroHitQueryDailyCount]
IDOLName=content
Operation=count
Field=queryinfo/numhits
NEqualStat=queryinfo/numhits,0
AEqualStat=action,query
Period=86400

[ContentZeroHitQueryWeeklyTop10Terms]
IDOLName=content
Operation=topn,10
Field=queryinfo/terms/term
NEqualStat=queryinfo/numhits,0
AEqualStat=action,query
Period=604800
Offset=2014/11/03 00:00:00

[QmsRulesHourlyTop100000]
IDOLName=qms
Operation=topn,100000
Field=queryinfo/results/reference
Period=3600
Offset=2014/11/03 00:00:00

[QmsRulesDailyTop100000]
IDOLName=qms
Operation=topn,100000
Field=queryinfo/results/reference
Period=86400
Offset=2014/11/03 00:00:00

[QmsRulesWeeklyTop100000]
IDOLName=qms
Operation=topn,100000
Field=queryinfo/results/reference
Period=604800
Offset=2014/11/03 00:00:00

//--------------------- Find Statistics (from required config) --------------//
[FindPaging10Minute]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=600

[FindPaging1Hour]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=3600

[FindPaging1Day]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=86400

[FindPaging1Week]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=604800

[FindPagingLifetime]
IDOLName=Find
Operation=CumulativeTopN,all
AEqualStat=find-event/type,page
DynamicField=find-event/search
Field=find-event/page
Offset=2016/04/11 00:00:00
Period=600

[FindAbandonmentPreview10Minute]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=600

[FindAbandonmentPreview1Hour]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=3600

[FindAbandonmentPreview1Day]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=86400

[FindAbandonmentPreview1Week]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=604800

[FindAbandonmentPreviewLifetime]
IDOLName=Find
Operation=CumulativeCount
AEqualStat=find-event/type,abandonment,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=600

[FindAbandonmentOriginal10Minute]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=600

[FindAbandonmentOriginal1Hour]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=3600

[FindAbandonmentOriginal1Day]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=86400

[FindAbandonmentOriginal1Week]
IDOLName=Find
Operation=Count
AEqualStat=find-event/type,abandonment,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=604800

[FindAbandonmentOriginalLifetime]
IDOLName=Find
Operation=CumulativeCount
AEqualStat=find-event/type,abandonment,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/type
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughPreview10Minute]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughPreview1Hour]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=3600

[FindClickthroughPreview1Day]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=86400

[FindClickthroughPreview1Week]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=604800

[FindClickthroughPreviewLifetime]
IDOLName=Find
Operation=CumulativeTopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughOriginal10Minute]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughOriginal1Hour]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=3600

[FindClickthroughOriginal1Day]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=86400

[FindClickthroughOriginal1Week]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=604800

[FindClickthroughOriginalLifetime]
IDOLName=Find
Operation=CumulativeTopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,original
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughFullPreview10Minute]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,full_preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

[FindClickthroughFullPreview1Hour]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,full_preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=3600

[FindClickthroughFullPreview1Day]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,full_preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=86400

[FindClickthroughFullPreview1Week]
IDOLName=Find
Operation=TopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,full_preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=604800

[FindClickthroughFullPreviewLifetime]
IDOLName=Find
Operation=CumulativeTopN,all
AEqualStat=find-event/type,clickthrough,find-event/click-type,full_preview
DynamicField=find-event/search
Field=find-event/position
Offset=2016/04/11 00:00:00
Period=600

//--------------------------- Logging ----------------------------------------//
[Logging] < "idol.common.cfg" [Logging]

0=ApplicationLogStream
1=QueryLogStream
2=EventLogStream

[ApplicationLogStream] < "idol.common.cfg" [ApplicationLogStream]
[QueryLogStream] < "idol.common.cfg" [QueryLogStream]
[EventLogStream] < "idol.common.cfg" [EventLogStream]
6. Verifying the Setup
After restarting the Statistics Server, you can check that events are being received and statistics are being calculated:

Check the EventLog – look for lines indicating events are being processed.

Query the Statistics Server via HTTP:

http://<StatsServerHost>:<ServicePort>/action=GetStatus – shows basic server health.

http://<StatsServerHost>:<ServicePort>/action=GetStatistics – lists all tracked metrics and their current values (use ?StatisticName=FindPaging10Minute to filter).

From Find, go to the statistics dashboard (if available) to see the aggregated data.

7. Troubleshooting
Issue	Likely Cause	Solution
No statistics appear	Events are not reaching the Statistics Server	Check that EventPort is open and that components are sending to the correct host/port. Verify EventClients includes the component IP.
Find metrics are empty	The EventField does not match the field in Find events	Ensure EventField matches the XML field that contains Find (or content, qms). If Find uses find-event/event, set EventField=find-event/event and adjust IDOLName accordingly.
Content/QMS stats missing after merge	The IDOLName in the metric sections does not match the IDOLName in the event	Verify each metric has IDOLName equal to the value sent in the event's identifier field.
Server fails to start	Duplicate section names or syntax errors	Check the configuration for duplicate [Statistics] indices or malformed lines.
8. Additional Notes
NiFi is not a direct event producer for the Statistics Server. If you want to track NiFi‑related metrics, consider using NiFi's own reporting tasks or integrating with external monitoring tools.

The Offset parameter defines when the cumulative statistics begin. Adjust it to your deployment's go‑live date if needed.

You can add more custom statistics by appending entries to the [Statistics] list and creating corresponding sections.

For further details, refer to the official IDOL QMS Administration Guide (Statistics Server chapter).

text

---

## Quick save instructions

| Platform | How to save |
|----------|-------------|
| **Linux** | Copy the content → open Notepad → paste → `File` → `Save As` → choose `All Files (*.*)` → name it `statistics-server-setup.md` |
| **macOS** | Copy the content → open TextEdit → `Format` → `Make Plain Text` → paste → `File` → `Save` → name it `statistics-server-setup.md` |
| **Linux** | Copy the content → open any text editor → paste → save as `statistics-server-setup.md` |

> 💡 **Tip**: If you're using VS Code, you can also create a new file, paste the content, and save it with the `.md` extension – it will render the Markdown nicely with preview support.
