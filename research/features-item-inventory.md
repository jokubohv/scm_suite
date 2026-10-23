# Item Record Management + Inventory Management Feature Catalog

Structured crawl of the two official NetSuite help books that define item-record and inventory/SCM backend behavior. This catalog feeds the executable feature-set plan (it is not the plan itself).

## Sources and coverage

- Item Record Management: <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2164398.html>
- Inventory Management: <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2249433.html>
- Crawl date: 2026-10-21
- Fetch count: 65 web_fetch calls (64 HTTP 200, 1 HTTP 404)
- Book TOC coverage: Item Record Management 9/9 top-level entries captured; Inventory Management 12/12 captured. Nested Advanced Pricing book (book_8135641320) captured 27/27 child topics; Quality Management part (part_1510239273) expanded through its 3 chapters.
- Chapter/section expansion: every book TOC chapter page was fetched; all immediate sub-topic links visible on those pages were captured (chapters that are pure landing pages were expanded one level deeper for key sections: costing, bins, transfers, counts, adjustments, AIM planning, quality, pricing, UoM, matrix/serial/lot).
- Depth: 2-5 levels (book > chapter > section > subsection > article). Deeper article bodies were not fetched once titles were captured; leaf summaries are therefore empty where the page was not fetched.
- Not expanded (by design): SCM Mobile book (book_161557401927) was never crawled; topics that reference SCM Mobile are classified `mobile`. Cross-book links that leave these two help areas (Purchasing, Manufacturing, Advanced Revenue Management, Accounting Preferences, Locations setup, CRM) were recorded only as dependency references, not expanded.
- Gaps: `section_N1515696627.html` (Inventory Status) returned HTTP 404 but is linked from both Inventory Management Setup and Bin Management; the Inventory Status capability is therefore evidenced through [Enabling Features for Inventory Management](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161963741628.html). No other 404s.

Counts: **412 topics**, **26 chapters/books**, **384 sections/articles**, domains: item-record-management 194, inventory-management 218.

## Item Record Management

Source: <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2164398.html>

*Item record management begins with creating item records for every item used on transactions. Item records carry pricing, costing, units of measure, bar codes, grouping behavior and customer part numbers.*
- **Using Item Records** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2164525.html> — *Item records define the goods and services a company buys and sells, including inventory, service and grouping items. The chapter introduces item creation, item visibility, web store display, multi-language names and related information.*
  - **Creating Item Records** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2166469.html> — *Items are goods sold to customers and parts purchased from vendors. Creating item records covers item type, custom form, item name/number and the subtabs holding purchasing, sales, accounting and preference data.*
    - Item Record Header Fields — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_161918187368.html>
    - Creating Item Records Using the Item Creation Assistant — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0414025056.html>
    - Copying and Importing Item Records — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2173669.html>
    - Featuring Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2585769.html>
    - Entering Purchasing and Inventory Information about Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2167714.html>
    - Sales and Shipping Information for Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2171993.html>
    - Account Information about Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2172688.html>
    - Revenue Recognition and Amortization Information about Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N2173064.html>
    - Tax and Tariff Information about Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N2173187.html>
    - Item Availability — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2656387.html>
    - Entering Preferences on Item Records — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2173412.html>
    - Item Configuration for Advanced Revenue Management — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4340443927.html>
    - Adding Components to Kits or Item Groups — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2170512.html>
    - Related Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2586873.html>
  - **Viewing the Items List** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2176664.html>
  - **Restricting Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2176374.html>
  - **Setting Up Items for the Web Site** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2585301.html>
  - **Working With Multi-Language Names and Descriptions** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2178279.html>
  - **Related Information for Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2179943.html>
- **Item Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2180614.html> — *Item pricing supports a single price or multiple price levels, quantity-based prices, quantity pricing schedules and pricing groups. Prices can be mass-updated, swapped between levels and printed as price lists.*
  - **Setting Up Item Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2181018.html> — *Pricing is configured on the item record Sales/Pricing subtab: quantity pricing schedule, marginal-rate and quantity-discount calculation modes (line, overall item, parent, schedule) and pricing group.*
    - Using Multiple Prices or Currencies — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_1493655077.html>
  - **Using Multiple Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2181607.html>
  - **Using Quantity Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2183333.html>
  - **Quantity Pricing Schedules** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2183677.html>
  - **Creating Pricing Groups** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2184077.html>
  - **Updating Item Prices** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2184312.html>
  - **Updating Item Purchase Prices** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2184741.html>
  - **Swapping Prices Between Price Levels** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2185052.html>
  - **Creating Item Coupons** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2185383.html>
  - **Generating Price Lists** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2185787.html>
- **Item Costing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2191369.html> — *Inventory costing tracks the cost of items through purchase and sale, posting to COGS and inventory asset accounts. It covers costing methods, initial average cost, decimal precision, costing preferences and recalculation behavior.*
  - **Setting a Default Inventory Costing Method** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4345703007.html>
  - **Costing Methods** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2191818.html> — *NetSuite supports Average (moving average), FIFO, LIFO, Group Average, Specific, Lot Numbered and Standard costing. The method is defined per item and cannot be changed after the item is saved.*
  - **Standard Costing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2199708.html> — *Standard costing maintains expected costs per cost category and tracks variances (purchase price, production quantity and cost, unbuilt) between standard and actual costs for manufacturers and distributors.*
    - Standard Costing Workflow — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_162705144712.html>
    - Standard Costing Example — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_162705257513.html>
    - Enabling Standard Costing — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2200714.html>
    - Creating Cost Categories — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2201059.html>
    - Creating Inventory Cost Templates — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159673804330.html>
    - Setting Up Item Records for Standard Costing — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2201836.html>
    - Defining Cost Versions — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2202327.html>
    - Entering Planned Standard Cost Records — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2202799.html>
    - Standard Cost Rollup — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2204939.html>
    - Revalue Standard Cost Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2205401.html>
    - Standard Costing and Transactions — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2206397.html>
    - Assembly Build Production Cost Variances — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2209083.html>
    - Standard Costing FAQ — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2211106.html>
    - Standard Costing Reporting — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2211461.html>
  - **Selecting a Default Cost of Goods Sold (COGS) Account** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2192814.html>
  - **Inventory Costing and Assembly Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2194190.html>
  - **LIFO/FIFO Inventory Costing and Advanced Receiving** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2194541.html>
  - **System Cost of Goods Sold Adjustments** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2195087.html>
  - **Setting Inventory Costing Preferences** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1497451045.html>
  - **Viewing Inventory Reports** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2197076.html>
  - **Inventory Costing Recalculations** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2197365.html> — *Costing recalculations adjust item costs when transactions are inserted into or removed from an existing transaction history, so averages and FIFO/LIFO layers stay consistent by transaction date.*
    - Inventory Costing Recalculations on Inventory Adjustments — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N2198841.html>
  - **Troubleshoot Inventory Costing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4447393386.html>
  - **Cost Accounting Status on Item Records** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2199228.html>
  - **Item Return Costing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2199328.html>
  - **Group Average Costing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4345703444.html> — *Group average costing tracks one average cost for an item across locations in a location costing group, dividing total group inventory value by total quantity. Requires Multi-Location Inventory.*
    - Enabling Group Average Costing — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_163231611059.html>
    - Creating a Location Costing Group — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4346482917.html>
    - Balance Location Costing Group Accounts — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4503902247.html>
    - Viewing the Balance Location Costing Group Accounts Status Page — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4514906066.html>
  - **Inventory Cost Accounting Workbook** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0420024853.html>
- **Multiple Units of Measure** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2211898.html> — *Multiple Units of Measure defines units types (for example Length: inch, foot, yard) with a base unit and converts purchase, stock, sale and consumption units on transactions and reports.*
  - **Enabling Multiple Units of Measure** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_163283384203.html>
  - **Setting Up Units of Measure** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2212143.html>
    - Assigning Units of Measure to Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2212390.html> — *Items are assigned a units type plus default purchase, stock, sale and consumption units; stock units govern quantity displays and reorder/available calculations, and non-inventory item types support limited unit sets.*
  - **Serial and Lot Inventory with Multiple Units of Measure** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2214525.html>
- **Bar Codes and Item Labels** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2215205.html> — *The Bar Coding and Item Labels feature generates UPC, EAN or Code 128 bar codes per item and per transaction, supports printing labels, and supports scanning to receive, fulfill, pick, pack, ship, bill or approve orders.*
  - **Using Code 128 Bar Codes** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_163292085605.html>
  - **Enabling the Bar Coding and Item Labels Feature** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_163292087805.html>
  - **Printing Bar Code and Item Labels** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2215536.html>
  - **Scanning Bar Codes** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2218627.html>
  - **Processing Orders Using Bar Codes** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N561778.html>
  - **Printing Labels From Transactions** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N562976.html>
- **Item Types** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2222944.html> — *Item types cover physical inventory (inventory, serial, lot, drop ship, special order, non-inventory), non-stocked records (service, download, gift certificate), grouping items (groups, kits, assemblies), transaction items and matrix items.*
  - **Item Records for Data Tracking** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_163300696254.html>
  - **Inventory Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2247990.html>
  - **Serial Numbered Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2230290.html> — *Serialized inventory assigns a serial number to each physical unit so a specific unit can be received, fulfilled, costed and traced. Serial numbers are entered on transactions and tracked per location.*
    - Enabling Serial Numbered Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2230646.html>
    - Entering Serialized Inventory Records — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2230957.html>
    - Track Serialized Inventory on Item Records — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2231339.html>
    - Add New Serial Numbers to Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2231739.html>
    - Adjusting Serialized Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2235043.html>
    - Receiving a Purchase Order With a Serialized Item — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2233116.html>
    - Serialized Items on Sales Transactions — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2233510.html>
    - Fulfilling a Sales Order with a Serialized Item — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2233933.html>
    - Memorizing Transactions with Serialized Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2234305.html>
    - Building a Serialized Assembly — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2234538.html>
    - Removing Serial Numbers on Case Forms — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2235383.html>
    - Searching for Serialized Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1508171622.html>
    - Customizing Lot or Serial Numbered Item Records — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2238963.html>
    - Avoiding Quantity Mismatches when Committing Numbered Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_96151731732.html>
  - **Lot Numbered Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2235684.html> — *Lot items track a group or quantity of items with a lot number plus expiration date and notes. Lot records carry quantity, expiry and specific cost as goods are purchased and sold.*
    - Enabling Lot Numbered Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_163360479619.html>
    - Creating Lot Numbered Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2236287.html>
    - Receiving a Purchase Order With a Lot Numbered Item — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2236584.html>
    - Selling and Fulfilling Lot Numbered Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2236900.html>
    - Building a Lot Numbered Assembly — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2238544.html>
    - Viewing Lot Numbered Inventory Item Records — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_163360798322.html>
    - Selecting Lot Numbers on Transactions — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_N2236107.html>
    - Lot Auto Numbering SuiteApp — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162780241331.html>
    - Allergen Statements SuiteApp — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1562672738.html>
  - **Drop Ship Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2239232.html>
  - **Special Order Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2242662.html>
  - **Non-Inventory Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2249092.html>
  - **Service Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248153.html>
  - **Download Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248348.html>
  - **Gift Certificates** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2244991.html>
  - **Item Groups** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2224824.html>
  - **Kit/Package Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2225190.html>
  - **Groups, Assemblies, and Kit/Packages** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2318089.html> — *Groups, assemblies and kit/packages combine multiple items into one sellable unit. Groups price from member prices, kits and assemblies price independently, and only assemblies are built/unbuilt with their own asset and COGS accounts.*
    - Assembly Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2319010.html>
  - **Matrix Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2227654.html> — *Matrix items track item combinations by options such as size and color using a parent item and child subitems. Matrices support up to 2000 combinations and can be created with the Matrix Item Assistant or custom lists/fields.*
    - Using the Matrix Item Assistant — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2227918.html>
    - Creating a Matrix Item Manually — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2228397.html>
    - Setting up an Item Matrix — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2228669.html>
    - Adding Items to a Matrix — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2229184.html>
    - Editing Matrix Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2229497.html>
    - Importing Matrix Options for Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N375701.html>
  - **Subtotal Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248793.html>
  - **Markup Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248975.html>
  - **Payment Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2249363.html>
  - **Other Charge Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2249208.html>
  - **Description Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248894.html>
  - **Discount Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248474.html>
  - **Expense Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4042372470.html>
- **Customer Part Number** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_4575965810.html> — *Customer Part Number, available with the Supply Chain Management SuiteApp, stores customer part numbers on inventory and assembly items so items can be added to sales orders/invoices and printed by customer part number.*
  - **Installing the Supply Chain Management SuiteApp** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4258666051.html>
  - **Set Up Customer Part Number** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4575966423.html>
  - **Customer Part Number Usage** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4576022723.html>
  - **Roles and Permissions for Customer Part Number** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4575970034.html>
  - **Import Customer Part Numbers for Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4575980825.html>
  - **Import Transactions With Customer Part Numbers** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4576567327.html>
- **Effective Date Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1536354469.html> — *The Effective Date Pricing SuiteApp schedules future item, item category, customer and customer group price changes, with a background script that applies and later reverts prices by effective start/end date.*
  - **Effective Date Pricing Process Flow** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1514442774.html>
  - **Required Features for Effective Date Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1532549817.html>
  - **Permissions Required for Effective Date Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1516013713.html>
  - **Installing Effective Date Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1514442792.html>
  - **Supported Item Types for Effective Date Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1532548320.html>
  - **Update Types for Creating Price Update Records** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1516009562.html>
  - **Creating a New Item Category** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_163482735047.html>
  - **Creating Price Update Records** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1516012245.html>
    - Creating an Item Price Update Record — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1536859410.html>
    - Creating an Item Category Price Update Record — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1536859987.html>
    - Creating a Customer Price Update Record — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1536860030.html>
    - Creating a Customer Group Price Update Record — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1536860062.html>
  - **Price Update Statuses in Effective Date Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1532549271.html>
  - **Testing the Effective Date Pricing Solution** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1536860962.html>
  - **Handling of Price Update Overlaps** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1536940864.html>
  - **Viewing Price Updates** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1530126293.html>
  - **Creating Price Detail Updates** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1516012501.html>
    - Creating a Price Detail Update for Item Type — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1521727587.html>
    - Creating a Price Detail Update for Item Category Type — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1521727688.html>
    - Creating a Price Detail Update for Customer Type — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1523469360.html>
    - Creating a Price Detail Update for Customer Group Type — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1523469385.html>
  - **Scheduling Automatic Price Updates** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1523897083.html>
  - **Import Price Updates Using CSV Files** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_160335109736.html>
  - **Limitations and Best Practices of Effective Date Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1537465810.html>
  - **Troubleshooting Unprocessed Updates** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1523897090.html>
- **Advanced Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_8135641320.html> — *Advanced Pricing activates price rules that apply price levels automatically by customer, item or date range, customer groups and cost-plus pricing at the price level, with supporting preferences and pricing workbooks.*
  - **Advanced Pricing Overview** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_9145542621.html>
  - **Enabling Advanced Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_9154128713.html>
  - **Advanced Pricing and the Item Record** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_3143342747.html>
  - **Setting Advanced Pricing Preferences** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_5154629643.html>
  - **Advanced Pricing Narrative Insights** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_9140950914.html>
  - **Creating Price Rules** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_1154809219.html>
  - **Setting Up Customer Groups** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_8102112633.html>
  - **Setting Up Price Levels in Advanced Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_6103534687.html>
  - **Editing a Price Rule** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2104514852.html>
  - **Deleting a Price Rule** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_5112017935.html>
  - **Updating Item Prices in Advanced Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_6112845511.html>
  - **Updating Prices in Advanced Pricing Using Mass Update** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_132401867.html>
  - **Importing CSV Files in Advanced Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2115039726.html>
  - **Managing Potential Conflicts in Advanced Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_7133941650.html>
  - **How Price Rules Use Dynamic Item Collections and Customer Groups** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_3143449978.html>
  - **Troubleshooting Price Rules in Advanced Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162043193.html>
  - **Effects of Disabling Advanced Pricing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_3104808100.html>
  - **Sales Pricing Overview Workbook** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2153411882.html>
  - **Sales Pricing Overview Workbook Details** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_122828838.html>
  - **Sales Pricing Overview Dataset Details** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2122854702.html>
  - **Price Variance by Item Pivot Details** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_9132749139.html>
  - **Sales by Price Level Pivot Details** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_3121437980.html>
  - **Sales by Price Rule Pivot Details** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_9151947846.html>
  - **Item Pricing Performance Workbook** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_5145051759.html>
  - **Item Pricing Performance Metrics Dataset Details** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2160901976.html>
  - **Item Pricing Performance Workbook Details** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_155557612.html>
  - **Historical Item Pricing Performance Dataset Details** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_9134028856.html>

## Inventory Management

Source: <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_N2249433.html>

*Inventory Management covers setup of inventory, locations and preferences, basic transactions (adjustments, counts, transfers, bins, sales and fulfillment), advanced demand-driven replenishment, consigned inventory, reporting and SuiteApps.*
- **Inventory Management Overview** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161970666917.html> — *Effective inventory management starts with accurate, accessible stock information. It explains basic versus advanced inventory management, the integrated inventory workflow and the main inventory reports.*
- **Inventory Management Setup** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2249539.html> — *Setup requires enabling inventory, location and order features, then creating item and location records. Preferences cover distribution, non-inventory conversion, level warnings, per-line locations, bins, transfers and counts.*
  - **Enabling Features for Inventory Management** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161963741628.html> — *Feature enablement depends on the setup: Inventory alone, Inventory with Locations, or Inventory with Multi-Location Inventory (adding Advanced Shipping and Receiving). Optional features add serial/lot, bins, counts, status and planning.*
  - **Inventory Setup with Locations and Multi-Locations** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2252794.html> — *Explains the three tracking models (inventory only, with locations, with multi-location) and the capabilities each unlocks: per-location stock, preferred locations, fulfillment, transfers, location reports and bins.*
  - **Distributing Inventory** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2304534.html> — *After enabling Multi-Location Inventory, existing stock sits in a null location and must be distributed to locations before it can be transacted. Simple distribution targets one location; manual splits quantity by location.*
    - Simple Inventory Distribution — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2304987.html>
    - Manual Inventory Distribution — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2305248.html>
  - **Converting Non-Inventory Items to Inventory Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2258046.html>
  - **Setting the Inventory Level Warnings Preference** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2254698.html>
  - **Non-Available Inventory Settings for Locations** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2307648.html>
  - **Configuring Per-Line Locations for Transactions** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162024160153.html>
  - **Setting Bin Preferences** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2273755.html>
  - **Setting Transfer Order Preferences** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2310077.html>
  - **Setting Up Inventory Count** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2297156.html> — *Inventory Count requires enabling the feature plus the Advanced Bin/Numbered Inventory Management feature for lot/serial items. Preferences set the default count variance account, snapshot recalculation on reject and display options.*
    - Setting Up Item Records for Inventory Count — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2297481.html>
  - **Inventory Allocation and Commitment Based on Status** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1517948058.html>
  - **Working with Item Location Attributes** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1504109154.html>
- **Basic Inventory Management** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2250682.html> — *Basic inventory management covers the core forms: assessing stock levels, adjusting inventory, moving inventory between locations, bins, replenishment, selling/fulfilling, committing and reallocating, and warehouse processing.*
  - **Inventory Workflow** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2251098.html> — *The inventory workflow cycle is purchasing, receiving, manufacturing, selling, fulfilling and replenishing. Each transaction updates inventory records in real time and reports immediate stock and value information.*
  - **Inventory Management with Locations** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162100850596.html> — *With the Locations feature, items and transactions are classified by location at the transaction body or per line. Location on transactions enables location-filtered reports and searches and location-based role restrictions.*
  - **Assessing Stock Levels** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2262573.html> — *Defines the quantity model used across NetSuite: quantity available, on hand, committed, on order, to order, back ordered, reorder point, preferred stock level and safety stock, and how to view them on items, reports and saved searches.*
    - Inventory Balance Search — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1518564230.html>
    - Inventory Detail Search — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1518565583.html>
  - **Reviewing Negative Inventory** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2268458.html>
  - **Inventory Adjustments** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2259648.html> — *Inventory levels can be changed with an Inventory Adjustment (incremental) or an Inventory Worksheet (absolute reset). Serialized and lot-numbered items can only use the Inventory Adjustment form.*
    - Entering an Inventory Adjustment — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161981111273.html>
    - Creating an Inventory Worksheet — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161981128590.html>
  - **Inventory Count** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2296970.html> — *Inventory Count snapshots on-hand quantity, records physical counts (calculated or manual) and, once approved, generates variance adjustments. Lot/serial counts need Advanced Bin/Numbered Inventory Management.*
    - Creating Calculated Inventory Counts — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2298951.html>
    - Creating Manual Inventory Counts — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2299331.html>
    - Working with an Inventory Count — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2299731.html>
  - **Item Settings and Stock Levels for Multi-Locations** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2305626.html> — *On item records, users set a preferred location, reorder point and preferred stock level per location, and view per-location on hand, on order, committed, in-transit quantity and value.*
  - **Multi-Location Inventory** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2303574.html> — *Multi-Location Inventory associates each item and transaction with a location, tracks purchase, sale, stock level and value per location, supports transfers between locations, and requires Advanced Receiving and Advanced Shipping.*
    - Returned-Item Costing Using Multi-Location Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2307937.html>
    - Advanced Item Location Configuration — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1503666392.html>
  - **Transferring Inventory** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2308202.html> — *Three ways to move stock between locations: a one-step basic inventory transfer, multi-step transfer orders with in-transit tracking, and intercompany transfer orders for OneWorld subsidiaries.*
    - Basic Inventory Transfers — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4660882963.html> — *A basic inventory transfer decreases quantity in the source location and increases it in the receiving location in one step. Serial numbers must match quantity and lot numbers use the LOT#(quantity) format.*
    - Inventory Transfer Orders — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2308766.html> — *Transfer orders schedule and track each stage of a transfer: approval commits stock and raises destination on-order, fulfillment removes stock from source and moves value to Inventory in Transit, and receipt restores destination on-hand.*
      - Entering a Transfer Order — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2310933.html>
      - Approving Transfer Orders — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2311649.html>
      - Fulfilling Transfer Orders — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2312176.html>
      - Pick, Pack, and Ship with Transfer Orders — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2312623.html>
      - Receiving Fulfilled Transfer Orders — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2312912.html>
      - Closing Transfer Orders — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2316558.html>
      - Searches, Reports, and Reminders for Transfer Orders — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2309511.html>
      - Customizing Transfer Orders — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2313259.html>
    - Intercompany Inventory Transfers - Non-Arm's Length — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2313577.html>
    - In-Transit Ownership — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4813066512.html>
    - Inventory Replenishment and Withdrawal — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2317004.html> — *Moves inventory between retail and storage locations to keep stock where demand is: replenish a location by inventory transfer or transfer order, or withdraw stock from multiple locations into one via the Withdraw by Transfer Order form.*
      - Replenish Location by Inventory Transfer — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4666984549.html>
      - Replenish Location by Transfer Order — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4666985845.html>
      - Withdrawing Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4667027605.html> — *The Withdraw by Transfer Order form pulls stock out of multiple locations into one destination, recommending transfer quantities and optional distribution-network source locations.*
  - **Bin Management** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2270284.html> — *Bin Management identifies storage areas within a location and tracks on-hand quantity per bin. Basic mode is quantity-only; Advanced Bin/Numbered Inventory Management adds serial/lot items, per-location bins and inventory detail.*
    - Basic Bin Management — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2271509.html>
    - Advanced Bin / Numbered Inventory Management — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2271791.html>
    - Enabling Bin Management Features — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2273046.html>
    - Creating Bin Records — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2274082.html>
    - Setting Up Item Records for Bins — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2274433.html>
    - Printing Bin Putaway Worksheets — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2275156.html>
    - Updating Bin Putaway Worksheets — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2277819.html>
    - Bin Transfers — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2278346.html>
    - Bin Management by Location — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4713998969.html>
    - Disabling Use Bins Settings and Features — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0512092419.html>
  - **Inventory Sales and Fulfillment** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2281204.html> — *Selling commits inventory, increasing quantity committed and decreasing available; fulfilling physical stock decreases on-hand and inventory asset value. Covers backorders, reallocation and underwater inventory.*
    - Selling Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2281482.html>
    - Fulfilling Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2281951.html>
    - Handling Backorders — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2263962.html>
    - Avoiding Underwater Inventory — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2264208.html>
  - **Committing Orders** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3765928460.html>
  - **Creating Commit Orders Schedule** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_4713119268.html>
  - **Reallocating Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2263567.html> — *The Reallocate Items page redistributes committed inventory across open orders, letting users commit, release and adjust quantities per order, with an Auto Commit option that allocates by order entry order.*
  - **Warehouse Processing** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2317586.html> — *Warehouse processing combines bar coding, manufacturing/assembly, bin management and inventory counts to receive, store, build and ship inventory items.*
  - **Ordering Items** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2403352.html>
- **Advanced Inventory Management** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2285050.html> — *AIM automates demand-based replenishment: lead time, safety stock and seasonal demand on item records drive reorder point and preferred stock level calculations, and underpin demand planning, DRP, supply planning, allocation and ATP.*
  - **Enabling the Advanced Inventory Management Feature** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162488513759.html> — *Enabling AIM on the Items & Inventory subtab automatically enables Bar Coding and Item Labels, Lot Tracking, Matrix Items, Pick Pack and Ship, Serialized Inventory and Multiple Units of Measure.*
  - **Inventory and Assembly Item Support** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_162212992098.html>
  - **Advanced Inventory Management FAQ** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4401903074.html>
  - **Allocate High Sales Order Volume** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1002093407.html>
  - **Setting Up Advanced Inventory Management** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2285514.html> — *Inventory Management Preferences set the defaults and intervals used by both AIM and Demand Planning: default lead time, safety stock, preferred stock level, order/seasonal analysis intervals, demand change, time fences and calculation day.*
    - Inventory Management Preferences — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162488535703.html>
    - Lead Time and Safety Stock Per Location — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2286205.html>
  - **Demand Planning** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2286970.html> — *Demand Planning analyzes stock demand and generates demand plans (by sales history or by current opportunities/quotes/orders) and supply plans that suggest purchase and work orders, considering lead times and safety stock.*
    - Setting Up Demand Planning — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2288536.html>
    - Calculating Item Demand — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2290234.html>
    - Manually Entering an Item Demand Plan — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2292418.html>
    - Viewing, Editing, and Deleting a Demand Plan — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2291961.html>
    - Creating Item Supply Plans — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2293372.html>
    - Manually Entering an Item Supply Plan — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2294552.html>
    - Viewing, Editing, and Deleting a Supply Plan — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2294140.html>
    - Creating Orders from Supply Plans — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2294794.html>
    - Mass Creating Work Orders — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2330082.html>
    - Reporting on Demand Planning — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2295256.html>
    - Demand Planning on Item Records — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2289082.html>
    - Monitoring the Demand Plan Status — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2291615.html>
    - Monitoring the Supply Plan Status — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2293790.html>
    - Distribution and Demand Planning — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2296427.html>
    - Demand Planning and Inventory Allocation — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3740808760.html>
    - Time Fences — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3848058018.html>
    - Planning Action Messages — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3865354301.html>
  - **Distribution Resource Planning** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049498070.html> — *DRP plans inventory across locations and subsidiaries using distribution categories, a network, bill of distribution and a Transfer supply type to recommend inter-location transfers. Superseded by Supply Planning planning rules.*
    - Setting Up Distribution Resource Planning — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049500343.html>
    - Creating a Distribution Category — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049501123.html>
    - Creating a Distribution Network — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049501559.html>
    - Creating a Bill of Distribution — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049502244.html>
    - Associating Distribution Categories — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049502746.html>
    - Generate Supply Plans Using DRP — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4060580900.html>
  - **Supply Planning** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159171867422.html> — *Supply Planning generates suggested purchase orders and work orders to fulfill demand plan requirements, using planning horizon, planning scope and planning rules; it produces workbench-based supply plans planners can review.*
    - Supply Planning Overview — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159172025754.html>
    - Setting Up Supply Planning — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159172130393.html>
    - Supply Planning Workbench — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159187932855.html>
    - SuiteAnalytics Supply Planning Workbooks — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0730021103.html>
  - **Supply Allocation** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156424975823.html> — *Supply Allocation matches supply to demand using a Supply Required By Date, allocation strategies and calculations, raising exceptions when demand cannot be met. Supports inventory, assembly and kit items plus FEFO lot assignment.*
    - Supply Allocation Setup — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_157073605250.html>
    - Supply Allocation Management — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156425243912.html>
    - SuiteAnalytics Supply Chain Workbook — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161400142423.html>
    - FEFO Lot Assignments — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_161009052865.html>
    - Setting Supply Required By Dates on Order Lines — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156639322112.html>
    - Creating Allocation Strategies — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156433189079.html>
    - Supply Allocation Calculations — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156556825274.html>
    - Supply Allocation Exceptions — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156518739289.html>
  - **Available to Promise** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2300269.html> — *Available to Promise projects the earliest ship date that the full ordered quantity will be available, based on expected receipts from purchase, work and transfer orders against expected shipments on sales and transfer orders.*
    - Available to Promise Methods — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2301636.html>
    - Enabling Available to Promise — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2300611.html>
    - Checking Item Availability — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2302738.html>
    - Available to Promise Earliest Availability — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159250317916.html>
    - Available to Promise in Demand Planning — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0929025924.html>
  - **Supply Chain Control Tower** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1519947103.html>
- **Consigned Inventory Management** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_94502112.html> — *Consigned Inventory Management tracks quantities and costs of vendor-consigned inventory, which is purchased, received, stocked, transferred and sold from a location but paid for only when sold, with ownership transfer automated.*
  - **Consigned Inventory Management Setup** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218102533.html>
  - **Purchasing and Receiving Consigned Inventory** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218113017.html>
  - **Consigned Inventory Adjustments** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218010438.html>
  - **Consigned Inventory Counts** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0327125707.html>
  - **Transferring Consigned Inventory** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218010714.html>
  - **Selling and Fulfilling Consigned Inventory** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218011109.html>
  - **Disabling Consigned Inventory Management** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0218110516.html>
  - **Journal Accounts for Vendor Consigned Inventory** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0317081004.html>
  - **Consignment Purchases Workbook** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0305095907.html>
  - **Generating Consignment Consumption Reports** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0305112108.html>
- **Inventory Reporting** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2353200.html> — *Inventory reports assess stock levels and value: stock ledger, physical inventory worksheet, snapshot, back order, activity detail, pending fulfillment, turnover, profitability, valuation and revenue reports.*
  - **Viewing the Stock Ledger** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4521145431.html>
  - **Inventory Level Assessments with Reports** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2354076.html>
  - **Physical Inventory Worksheet** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2354418.html>
  - **Current Inventory Snapshot Report** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2355248.html>
  - **Inventory Back Order Report** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2355658.html>
  - **Inventory Activity Detail Report** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2355966.html>
  - **Items Pending Fulfillment Report** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2356254.html>
  - **Inventory Turnover Report** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2356560.html>
  - **Inventory Value Assessments with Reports** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2358468.html>
  - **Inventory Profitability Report** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2358569.html>
  - **Inventory Valuation Summary Report** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2358933.html>
  - **Inventory Valuation Detail Report** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2359248.html>
  - **Inventory Revenue Report** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2359520.html>
  - **Inventory Revenue Detail Report** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2359826.html>
- **Inventory SuiteAnalytics Workbooks** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_156708335167.html> — *SuiteAnalytics provides inventory dataset and workbook templates with predefined source data and criteria, including table views, pivot tables and charts.*
  - **Inventory Dataset Templates** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_158219102920.html>
  - **Inventory Workbook Templates** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156708364339.html>
- **Warranty and Repairs Management SuiteApp** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_3938292477.html> — *Warranty and Repairs Management registers item warranties (auto from invoices, manual or imported), tracks default warranty data on items, validates claims against warranty terms and processes refund/repair/replacement claims.*
  - **Warranty and Repairs Management Setup** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3938292683.html>
  - **Warranty Preferences and Item Setup** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3938299291.html>
  - **Warranty Registration** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3938315832.html>
  - **Warranty Claims** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_3938319263.html>
  - **Return Authorizations for Warranty Claims** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4391752984.html>
  - **Managing Refund, Repair, and Replacement Claims** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4391753773.html>
  - **Warranty and Repairs Management Considerations** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/bridgehead_4389346541.html>
- **Quality Management** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/part_1510239273.html> — *The Quality Management SuiteApp provides templates and tools to maintain and associate quality data with business records: quality inspections and specifications, triggers, queues, COA and quality reporting.*
  - **Quality Management Overview** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1501602139.html> — *Quality Management helps meet regulatory and internal quality expectations by formalizing and assessing inventory and process standards, tying quality data to other records and workflows.*
    - The Quality Tab — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0718100342.html>
  - **Quality Management Administration** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_158403489694.html> — *Administration covers roles, quality process flow, prerequisites, customizable workflows, Quality Management Connect, plugins and the Quality Management REST API.*
    - Quality Management Roles — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_158403724627.html>
    - Quality Process Flow — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_158403788313.html>
    - Quality Management Prerequisites — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1528995922.html>
    - Customizing Quality Management Workflows — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1524583615.html>
    - Quality Management Connect — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159594269535.html>
    - NetSuite Plugins — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_158627748777.html>
    - Quality Management REST API — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1540583465.html>
  - **Quality Management User Guide** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1503070323.html> — *A quality process is defined by inspections and specifications. Covers inspections, specifications, triggers, inspection queues, workflows, certificate of analysis, derived fields, preferences, mobile data collection and reports.*
    - Quality Management Glossary — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1498594076.html>
    - Quality Management Best Practices — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0715122351.html>
    - Quality Inspections — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1525966493.html>
    - Quality Specifications — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1525966601.html>
    - Quality Management Triggers — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0609085939.html>
    - On-Demand Quality Inspection Queues — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_164133074791.html>
    - Quality Management Saved Searches — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_164133163593.html>
    - Quality Management Workflows — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_164133175329.html>
    - Store Quality Management Images — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1525966687.html>
    - Resetting Transaction Count — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1525966741.html>
    - Certificate of Analysis (COA) — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_163274330949.html>
    - Derived Fields — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_163274428926.html>
    - Configure Quality Management Preferences — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_163708622240.html>
    - Quality Management Mobile Data Collection — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1503422114.html>
    - Quality Management Reports — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1495134914.html>
- **Auto Assign UPC** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_158373836991.html> — *The Auto Assign UPC SuiteApp automatically assigns UPCs to new inventory and assembly items (standard and matrix), links UPC records to items, tracks assignment/retirement dates, and provides saved searches and scheduled automation.*
  - **Setting Up Auto Assign UPC** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_158573839837.html>
  - **Working with Auto Assign UPC** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_158590007619.html>
  - **Monitoring UPCs and Disabling Automatic Assignment** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_158737099515.html>
- **Smart Count** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_83123952246.html> — *The Smart Count SuiteApp supports real-time inventory counts during business hours with directed or spot counts, blind counts, tolerances, variance auto-approval, recounts and a count dashboard; it works with the SCM Mobile SuiteApp.*
  - **Smart Count Supported Languages** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_10123024982.html>
  - **Installing the Smart Count SuiteApp** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_29113559384.html>
  - **Smart Count Roles and Permissions** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0620115020.html>
  - **Setting Up Smart Count Preferences** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_46154957887.html>
  - **Working with Smart Count SuiteApp** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_1222054411.html>
  - **Item Count Scenarios** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0701030012.html>
  - **Smart Count Scripts** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0701030138.html>
  - **Smart Count Saved Searches** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0701030155.html>
  - **Smart Count Error Messages** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0816013247.html>
  - **Uninstalling the Smart Count SuiteApp** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0701030216.html>
  - **Creating and Managing Count Configurations** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0620044529.html>
  - **Performing Inventory Count on Mobile Devices** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_49120505206.html>
  - **Working with Smart Count Review** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_0620044825.html>
  - **Item Count Features** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_2145847230.html>
  - **GS1 Barcode Scanning for Inventory Counts** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_1222055829.html>
- **Item 360 Dashboard** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2145345562.html> — *The Item 360 Dashboard SuiteApp gives a consolidated real-time view of an item across transaction types and locations, plus an Item Alerts dashboard for supply planning, order delay and supply allocation alerts.*
  - **Installing Item 360 Dashboard** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1121012616.html>
  - **Creating Criteria Configuration for Item 360 Dashboard** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1121030646.html>
  - **Item Alerts Dashboard** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1121041335.html>
  - **Item 360 Dashboard Portlets** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1121042611.html>
  - **Error Messages for Item 360 Dashboard** — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1122012331.html>

## Cross-cutting backend capabilities observed

Each capability below is a concrete backend behavior seen in the two books, with a dependency hint ("needs first") and the best NetSuite help link. Ordering follows a plausible build sequence: item master and transaction skeleton first, then costing/pricing, then locations/bins/counts/transfers, then planning and SuiteApp refinements.

- **Inventory transaction skeleton** — Every inventory change must post quantity effects per item/location and GL value effects to inventory asset / COGS accounts; the docs describe a closed cycle of purchasing, receiving, manufacturing, selling, fulfilling and replenishing where each transaction updates records immediately. *Needs first: Items + locations + GL mapping.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2251098.html)
- **Item record data model** — Item records drive everything: item type, name/number, costing method, units type, price levels, tax/revenue accounts, purchasing and inventory subtabs, preferences and per-location settings. Several fields (costing method, units type, initial average cost) are immutable after save. *Needs first: nothing (foundation).* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2166469.html)
- **Item type taxonomy** — Distinct behavior per type: inventory, serial-numbered, lot-numbered, non-inventory, drop ship, special order, service, download, gift certificate, group, kit/package, assembly, matrix, subtotal/markup/payment/other-charge/description/discount/expense items. *Needs first: item record data model.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2222944.html)
- **Inventory quantity model** — Canonical derived quantities: quantity on hand, committed, available (on hand minus committed, never negative), on order, to order (preferred stock level minus available), back ordered, in transit, allocated. Any implementation needs a single source of truth for these. *Needs first: inventory transaction skeleton.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2262573.html)
- **Multiple units of measure (UoM)** — Units types with a base unit and stock/purchase/sale/consumption units per item; conversion on transactions, reorder/availability display in stock units, and separate unit rules for non-inventory items. *Needs first: item record data model.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2211898.html)
- **Matrix items** — Parent item plus child subitems across option combinations (up to 2000); per-combination records, price/qty behavior across siblings (by parent quantity) and CSV import of matrix options. *Needs first: item master + custom lists/fields.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2227654.html)
- **Groups / kits / assemblies** — Group members sum to the group price and decrement member stock on sale; kits price independently and decrement members; assemblies are built/unbuilt with member stock changes, own asset/COGS accounts and an assembly build cost. *Needs first: item master + inventory transaction skeleton.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2318089.html)
- **Costing methods** — Per-item costing method: Average (moving average), FIFO, LIFO, Group Average, Specific (serial), Lot Numbered, Standard. Method is immutable after item save; negative inventory falls back to last purchase price. *Needs first: inventory transaction skeleton.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2191818.html)
- **Costing recalculation** — Insert/remove dated transactions triggers recalculation of item cost history (average/FIFO layers) and system COGS adjustments; cost accounting status is exposed on item records. *Needs first: costing methods.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2197365.html)
- **Standard costing and variances** — Cost categories, inventory cost templates, planned standard cost records, cost versions, rollup, revaluation and purchase-price/production/unbuilt variance tracking. *Needs first: costing + assembly items + GL.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2199708.html)
- **Group average costing** — One average cost per item across the locations of a location costing group; requires Multi-Location Inventory, recalculates on every costing-impacting transaction, handles underwater fulfillment and can include in-transit value. *Needs first: costing + multi-location.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4345703444.html)
- **Price levels and quantity pricing** — Per-item price levels (retail/wholesale/etc.), quantity pricing with marginal rates and quantity computation modes (line, overall item, parent, schedule), quantity pricing schedules as templates, and pricing groups that map customer groups to item groups. *Needs first: item master + customer records.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2180614.html)
- **Pricing maintenance and coupons** — Mass update of sales/purchase prices, swapping prices between levels, generated price lists, and item-specific coupon codes when promotion codes are enabled. *Needs first: price levels.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2184312.html)
- **Advanced pricing (price rules)** — Price rules automatically select price levels by customer/customer group/item/date criteria; cost-plus pricing with cost basis and markup/discount at the price level; conflict handling and CSV import. *Needs first: price levels + customer groups.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/book_8135641320.html)
- **Effective-date / scheduled pricing** — SuiteApp that stores future price updates on item, item category, customer and customer group records and applies/reverts them by effective start/end date via scheduled script, with overlap handling. *Needs first: price levels + item categories/customer groups.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1536354469.html)
- **Locations and multi-location inventory** — Location records (optional hierarchy), per-item preferred location, per-location stock levels/value, location on transactions or per line, location-filtered reporting and location-based role restrictions. *Needs first: inventory feature.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2303574.html)
- **Inventory distribution** — Bootstraps multi-location: unallocated (null-location) stock must be distributed to one or many locations before transacting; simple vs manual distribution; negative inventory must be resolved first. *Needs first: multi-location inventory.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2304534.html)
- **Bins and advanced bins** — Bin records as storage areas within a location; on-hand quantity tracked per bin (not cost/committed per bin); putaway worksheets, preferred bins, required bins on sales/negative adjustments, bin transfers; advanced mode adds serial/lot bins, per-location bins and the inventory detail subrecord. *Needs first: locations (+ multi-location for per-location bins).* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2270284.html)
- **Inventory status** — Statuses assigned to stock that can make inventory unavailable for allocation and drive internal processes (only with advanced bins/numbered inventory). The dedicated help page 404s; behavior is described in feature enablement. *Needs first: bin management / advanced bin-numbered inventory.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161963741628.html)
- **Inventory adjustments** — Incremental Inventory Adjustment vs absolute Inventory Worksheet (sets count, not delta); serialized/lot items can only use adjustments; adjustment accounts post the value effect. *Needs first: inventory transaction skeleton.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2259648.html)
- **Inventory counts** — Calculated vs manual counts, snapshot of on-hand at count start, count review/approve/reject, variance generation into adjustments, snapshot recalculation preference, next count date/interval per item, lot/serial counts require advanced bin-numbered management. *Needs first: inventory + item count dates (+ advanced bins for numbered items).* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2296970.html)
- **Basic inventory transfers** — Single-step transfer reduces source on-hand and increases destination on-hand; serial numbers must equal quantity; lot entry uses LOT#(qty); bin-aware from/to bins; >1000 lines discouraged. *Needs first: multi-location inventory + bins if used.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4660882963.html)
- **Transfer orders** — Two-step scheduled transfer with approval, commitment from source, destination on-order increase, fulfillment into inventory-in-transit GL, receipt to destination, close; lot/serial entry rules; transfer price; location-restricted role behavior; intercompany (OneWorld) variant. *Needs first: multi-location inventory + GL in-transit accounts.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2308766.html)
- **Replenishment and withdrawal worksheets** — Replenish a location by transfer or transfer order, and withdraw stock from multiple locations into one destination, optionally driven by the DRP bill of distribution and lead times; creates transfer orders in bulk. *Needs first: transfer orders + reorder points.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2317004.html)
- **Reorder points and preferred stock levels** — Per-item and per-location reorder point, preferred stock level and safety stock; quantity-to-order and Order Items workflow; inventory level warning preference; reorder reminders. *Needs first: multi-location + item stock settings.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2305626.html)
- **Advanced Inventory Management (AIM)** — Automated demand-based replenishment: item lead time, safety stock and seasonal demand; NetSuite auto-calculates reorder points and preferred stock levels nightly/weekly; settings revert to manual values when auto-calc is disabled. *Needs first: multi-location + transaction history.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2285050.html)
- **Demand planning** — Demand plans from sales history projection (linear regression, moving average, seasonal average, sales forecast) or from open opportunities/quotes/orders; supply plans suggest purchase and work orders honoring lead time and safety stock; multi-tier assembly planning. *Needs first: AIM.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2286970.html)
- **Supply planning / MRP** — Suggested purchase orders and work orders from planning horizon, planning scope and planning rules, reviewed in the Supply Planning Workbench; planning rules define transfer behavior across the supply chain. *Needs first: AIM + demand planning.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159171867422.html)
- **Distribution resource planning (DRP)** — Distribution categories, distribution network, bill of distribution and a Transfer supply type to plan inter-location transfers across subsidiaries; superseded by Supply Planning planning rules but still documented. *Needs first: AIM + multi-location inventory.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4049498070.html)
- **Supply allocation** — Matches supply sources to demand by Supply Required By Date using allocation strategies, automatic/scheduled calculations, allocation exceptions, FEFO lot assignment; supports inventory, assembly and kit items. *Needs first: AIM + demand/supply orders.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156424975823.html)
- **Available to promise** — Projects earliest date the full ordered quantity is available by netting expected receipts (PO/WO/TO) against expected shipments (SO/TO) per day; exposed on quotes and orders via Check Item Availability. *Needs first: demand planning / projected supply.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2300269.html)
- **Order commitment and reallocation** — Automatic commitment on order entry/approval and on receipt/assembly; reallocate committed stock across open orders with commit/release per line and auto-commit by order-entry order. *Needs first: inventory quantities + sales orders.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2263567.html)
- **Lot tracking** — Lot numbers group quantities with expiry date and notes, specific lot cost, lot selection on purchase/sales/transfer transactions, lot-numbered assembly builds, lot custom fields and searching by lot. *Needs first: inventory + lot tracking feature.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2235684.html)
- **Serial tracking** — Per-unit serial numbers with specific cost, per-location status, receiving/fulfilling by serial, serialized assembly builds, customer serial history and custom serial fields. *Needs first: inventory + serialized inventory feature.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2230290.html)
- **Advanced bin/numbered inventory + inventory detail** — The inventory detail subrecord captures bin/lot/serial/status per transaction line; enables numbered inventory in bins and quantity-by-status tracking. *Needs first: bins + lot/serial.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2271791.html)
- **Sales and fulfillment inventory effects** — Selling commits and reduces available (backorders when short); fulfillment reduces on-hand and asset value; backorder reports and underwater inventory avoidance. *Needs first: inventory quantities + orders.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2281204.html)
- **Consigned inventory** — Vendor consignment with 'Allow Vendor Consignment' items, separate consigned vs non-consigned quantities/values, consigned inventory status, consigned adjustments/counts/transfers, and ownership transfer when sold; assembly items unsupported. *Needs first: multi-location + inventory status.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_94502112.html)
- **Warehouse processing** — Combines bar-coded identification, assembly/kit build, bins and counts into a receive-store-build-ship warehouse flow. *Needs first: bins + barcodes + counts.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2317586.html)
- **Bar coding and labels** — UPC/EAN/Code 128 bar codes generated from item name/number or SKU, per-item format choice, label printing, serial/lot bar codes, and scanning of item and transaction bar codes for bulk receive/fulfill/pick/pack/ship/bill/approve. *Needs first: item master (+ lot/serial for numbered).* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2215205.html)
- **Customer part numbers** — SCM SuiteApp mapping of customer part numbers to inventory/assembly items, used to add and print items on sales orders/invoices, with import support and role permissions. *Needs first: items + customer records.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_4575965810.html)
- **Warranty and repairs (SuiteApp)** — Warranty registration (auto from invoice, manual, CSV/web service), default warranty data on items, claim validation against registrations, and RA/credit-memo/sales-order flows for refund/repair/replacement. *Needs first: items + invoices + return authorizations.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_3938292477.html)
- **Quality management (SuiteApp)** — Quality inspections and specifications attached to transactions/items, triggers, on-demand inspection queues, workflows, certificate of analysis, derived fields, REST API and quality reporting. *Needs first: items + transactions + SuiteApp.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/part_1510239273.html)
- **Smart count (SuiteApp)** — Real-time directed/spot counts, blind counts, tolerances and auto-approval by item/bin/status, recount and review workflow, GS1 barcode scanning, works with SCM Mobile. *Needs first: inventory count + bins + SCM Mobile.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_83123952246.html)
- **Auto assign UPC (SuiteApp)** — Automatic UPC assignment and retirement for new inventory/assembly items (incl. matrices), UPC-to-item linkage, monitoring saved searches and scheduled automation. *Needs first: item master.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_158373836991.html)
- **Item 360 dashboard (SuiteApp)** — Consolidated real-time per-item view across locations/subsidiaries plus supply planning, order delay and supply allocation alerts; limit ~200k items. *Needs first: all inventory data + SuiteApp.* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2145345562.html)
- **Inventory reporting and analytics** — Stock ledger, inventory snapshot, back-order, activity detail, pending fulfillment, turnover, profitability, valuation summary/detail, revenue reports plus SuiteAnalytics inventory dataset/workbook templates. *Needs first: all inventory transactions (read models).* [Doc](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2353200.html)

## Classified out of core backend phase

Topics whose `backend_relevance` is `ui-only`, `mobile`, `analytics`, `suiteapp`, `integration` or `unclear`. These are listed in the JSON with the same classification and can be deferred or handled in later phases. For compactness, only the out-of-core subtree roots are shown with the number of topics each subtree contains.

### ui-only (12 topics, 7 subtree roots)

- Viewing the Items List — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2176664.html> (1 topics)
- Related Information for Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2179943.html> (1 topics)
- Generating Price Lists — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2185787.html> (1 topics)
- Printing Bar Code and Item Labels — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2215536.html> (1 topics)
- Printing Labels From Transactions — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N562976.html> (1 topics)
- Supply Chain Control Tower — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1519947103.html> (1 topics)
- Item 360 Dashboard — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2145345562.html> (6 topics)

### mobile (2 topics, 2 subtree roots)

- Quality Management Mobile Data Collection — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1503422114.html> (1 topics)
- Performing Inventory Count on Mobile Devices — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_49120505206.html> (1 topics)

### analytics (36 topics, 20 subtree roots)

- Viewing Inventory Reports — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2197076.html> (1 topics)
- Inventory Cost Accounting Workbook — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0420024853.html> (1 topics)
- Sales Pricing Overview Workbook — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2153411882.html> (1 topics)
- Sales Pricing Overview Workbook Details — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_122828838.html> (1 topics)
- Sales Pricing Overview Dataset Details — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2122854702.html> (1 topics)
- Price Variance by Item Pivot Details — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_9132749139.html> (1 topics)
- Sales by Price Level Pivot Details — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_3121437980.html> (1 topics)
- Sales by Price Rule Pivot Details — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_9151947846.html> (1 topics)
- Item Pricing Performance Workbook — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_5145051759.html> (1 topics)
- Item Pricing Performance Metrics Dataset Details — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2160901976.html> (1 topics)
- Item Pricing Performance Workbook Details — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_155557612.html> (1 topics)
- Historical Item Pricing Performance Dataset Details — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_9134028856.html> (1 topics)
- Reporting on Demand Planning — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2295256.html> (1 topics)
- SuiteAnalytics Supply Planning Workbooks — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0730021103.html> (1 topics)
- SuiteAnalytics Supply Chain Workbook — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_161400142423.html> (1 topics)
- Consignment Purchases Workbook — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0305095907.html> (1 topics)
- Generating Consignment Consumption Reports — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0305112108.html> (1 topics)
- Inventory Reporting — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_N2353200.html> (15 topics)
- Inventory SuiteAnalytics Workbooks — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_156708335167.html> (3 topics)
- Quality Management Reports — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1495134914.html> (1 topics)

### suiteapp (81 topics, 8 subtree roots)

- Lot Auto Numbering SuiteApp — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_162780241331.html> (1 topics)
- Allergen Statements SuiteApp — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1562672738.html> (1 topics)
- Customer Part Number — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_4575965810.html> (7 topics)
- Effective Date Pricing — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1536354469.html> (26 topics)
- Warranty and Repairs Management SuiteApp — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_3938292477.html> (8 topics)
- Quality Management — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/part_1510239273.html> (27 topics)
- Auto Assign UPC — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_158373836991.html> (4 topics)
- Smart Count — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_83123952246.html> (16 topics)

### integration (10 topics, 10 subtree roots)

- Creating Item Records Using the Item Creation Assistant — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_0414025056.html> (1 topics)
- Copying and Importing Item Records — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2173669.html> (1 topics)
- Importing Matrix Options for Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N375701.html> (1 topics)
- Import Customer Part Numbers for Items — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4575980825.html> (1 topics)
- Import Transactions With Customer Part Numbers — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4576567327.html> (1 topics)
- Import Price Updates Using CSV Files — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_160335109736.html> (1 topics)
- Importing CSV Files in Advanced Pricing — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_2115039726.html> (1 topics)
- Quality Management Connect — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_159594269535.html> (1 topics)
- NetSuite Plugins — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_158627748777.html> (1 topics)
- Quality Management REST API — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1540583465.html> (1 topics)

### unclear (1 topics, 1 subtree roots)

- Advanced Pricing Narrative Insights — <https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/article_9140950914.html> (1 topics)

## Counts

| Metric | Value |
| --- | --- |
| Total topics | 412 |
| books | 3 |
| chapters / parts | 23 |
| sections | 326 |
| articles | 58 |
| domain: item-record-management | 194 |
| domain: inventory-management | 218 |
| backend_relevance: core-backend | 270 |
| backend_relevance: ui-only | 12 |
| backend_relevance: mobile | 2 |
| backend_relevance: analytics | 36 |
| backend_relevance: integration | 10 |
| backend_relevance: suiteapp | 81 |
| backend_relevance: unclear | 1 |

Per book: Item Record Management subtree 194 topics (including nested Advanced Pricing book and Effective Date Pricing); Inventory Management subtree 218 topics (including Quality Management part).

Notes: no topics were invented — every entry was taken from a book TOC, chapter landing page, or captured section/article page link. JSON validity, unique URLs and all counts in this file were verified by an independent validation pass. Leaves whose page body was not fetched intentionally carry an empty `summary`.
