# Prices Tab Setup

Paste the following into your Google Sheet "Prices" tab starting at cell A1.
Column A = your ticker label, Column B = GoogleFinance symbol, Column C = live price formula, Column D = prev close formula, Column E = currency

| A       | B                    | C                                  | D                                       | E   |
|---------|----------------------|------------------------------------|-----------------------------------------|-----|
| Ticker  | GF_Symbol            | Price                              | PrevClose                               | Currency |
| SPXL    | SPXL                 | =GOOGLEFINANCE(B2,"price")         | =GOOGLEFINANCE(B2,"closeyest")          | USD |
| FNGU    | FNGU                 | =GOOGLEFINANCE(B3,"price")         | =GOOGLEFINANCE(B3,"closeyest")          | USD |
| NVDA    | NVDA                 | =GOOGLEFINANCE(B4,"price")         | =GOOGLEFINANCE(B4,"closeyest")          | USD |
| TXF.TO  | TSE:TXF              | =GOOGLEFINANCE(B5,"price")         | =GOOGLEFINANCE(B5,"closeyest")          | CAD |
| TSLA    | TSLA                 | =GOOGLEFINANCE(B6,"price")         | =GOOGLEFINANCE(B6,"closeyest")          | USD |
| CM.TO   | TSE:CM               | =GOOGLEFINANCE(B7,"price")         | =GOOGLEFINANCE(B7,"closeyest")          | CAD |
| UDOW    | UDOW                 | =GOOGLEFINANCE(B8,"price")         | =GOOGLEFINANCE(B8,"closeyest")          | USD |
| ENB.TO  | TSE:ENB              | =GOOGLEFINANCE(B9,"price")         | =GOOGLEFINANCE(B9,"closeyest")          | CAD |
| TSM     | TSM                  | =GOOGLEFINANCE(B10,"price")        | =GOOGLEFINANCE(B10,"closeyest")         | USD |
| RY.TO   | TSE:RY               | =GOOGLEFINANCE(B11,"price")        | =GOOGLEFINANCE(B11,"closeyest")         | CAD |
| IBKR    | IBKR                 | =GOOGLEFINANCE(B12,"price")        | =GOOGLEFINANCE(B12,"closeyest")         | USD |
| AVGO    | AVGO                 | =GOOGLEFINANCE(B13,"price")        | =GOOGLEFINANCE(B13,"closeyest")         | USD |
| COST    | COST                 | =GOOGLEFINANCE(B14,"price")        | =GOOGLEFINANCE(B14,"closeyest")         | USD |
| BMO.TO  | TSE:BMO              | =GOOGLEFINANCE(B15,"price")        | =GOOGLEFINANCE(B15,"closeyest")         | CAD |
| RDS     | RDS                  | =GOOGLEFINANCE(B16,"price")        | =GOOGLEFINANCE(B16,"closeyest")         | USD |
| LYV     | LYV                  | =GOOGLEFINANCE(B17,"price")        | =GOOGLEFINANCE(B17,"closeyest")         | USD |
| NFLX    | NFLX                 | =GOOGLEFINANCE(B18,"price")        | =GOOGLEFINANCE(B18,"closeyest")         | USD |
| GBTC    | GBTC                 | =GOOGLEFINANCE(B19,"price")        | =GOOGLEFINANCE(B19,"closeyest")         | USD |
| V       | V                    | =GOOGLEFINANCE(B20,"price")        | =GOOGLEFINANCE(B20,"closeyest")         | USD |
| ET      | ET                   | =GOOGLEFINANCE(B21,"price")        | =GOOGLEFINANCE(B21,"closeyest")         | USD |
| AAPL    | AAPL                 | =GOOGLEFINANCE(B22,"price")        | =GOOGLEFINANCE(B22,"closeyest")         | USD |
| QCOM    | QCOM                 | =GOOGLEFINANCE(B23,"price")        | =GOOGLEFINANCE(B23,"closeyest")         | USD |
| MSFT    | MSFT                 | =GOOGLEFINANCE(B24,"price")        | =GOOGLEFINANCE(B24,"closeyest")         | USD |
| MSTR    | MSTR                 | =GOOGLEFINANCE(B25,"price")        | =GOOGLEFINANCE(B25,"closeyest")         | USD |
| BYDDF   | BYDDF                | =GOOGLEFINANCE(B26,"price")        | =GOOGLEFINANCE(B26,"closeyest")         | USD |
| USDCAD  | CURRENCY:USDCAD      | =GOOGLEFINANCE(B27,"price")        | =GOOGLEFINANCE(B27,"closeyest")         | CAD |
