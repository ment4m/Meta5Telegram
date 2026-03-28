//+------------------------------------------------------------------+
//|  TelegramCompoundEA.mq5  v1.1                                    |
//|  Single-trade compounding EA.                                    |
//|  Risk = RiskPercent% of balance. TP = ProfitPercent% of balance.|
//|  Both calculated fresh at trade open — ignores signal TP.        |
//+------------------------------------------------------------------+
#property copyright "MyTradeBot"
#property version   "1.10"
#property strict

#include <Trade\Trade.mqh>

CTrade trade;

input int    CheckIntervalMs = 1000;
input string SignalFolder    = "signals_compound";   // separate folder from main EA
input int    MagicNumber     = 235000;               // different magic from TelegramSignalEA
input double RiskPercent     = 23.0;                 // % of balance to risk (SL hit = lose this %)
input double ProfitPercent   = 30.0;                 // % of balance to target (TP hit = gain this %)
input int    Deviation       = 20;

//+------------------------------------------------------------------+
//| JSON helpers (same as main EA)                                   |
//+------------------------------------------------------------------+
string JsonGetString(const string &json, const string key)
{
    string search = "\"" + key + "\"";
    int pos = StringFind(json, search);
    if(pos < 0) return "";
    pos = StringFind(json, ":", pos);
    if(pos < 0) return "";
    pos = StringFind(json, "\"", pos);
    if(pos < 0) return "";
    pos++;
    int end = StringFind(json, "\"", pos);
    if(end < 0) return "";
    return StringSubstr(json, pos, end - pos);
}

double JsonGetDouble(const string &json, const string key)
{
    string search = "\"" + key + "\"";
    int pos = StringFind(json, search);
    if(pos < 0) return -999999.0;
    pos = StringFind(json, ":", pos);
    if(pos < 0) return -999999.0;
    pos++;
    while(pos < StringLen(json) && StringSubstr(json, pos, 1) == " ") pos++;
    if(StringSubstr(json, pos, 4) == "null") return -999999.0;
    string numStr = "";
    while(pos < StringLen(json))
    {
        string ch = StringSubstr(json, pos, 1);
        if(ch == "," || ch == "}" || ch == "\n" || ch == "\r" || ch == " ") break;
        numStr += ch;
        pos++;
    }
    StringTrimRight(numStr);
    if(StringLen(numStr) == 0) return -999999.0;
    return StringToDouble(numStr);
}

string JsonGetArray(const string &json, const string key)
{
    string search = "\"" + key + "\"";
    int pos = StringFind(json, search);
    if(pos < 0) return "";
    pos = StringFind(json, "[", pos);
    if(pos < 0) return "";
    int end = StringFind(json, "]", pos);
    if(end < 0) return "";
    return StringSubstr(json, pos, end - pos + 1);
}

int ParseTPs(const string &arr, double &tps[])
{
    ArrayResize(tps, 0);
    if(StringLen(arr) <= 2) return 0;
    string inner = StringSubstr(arr, 1, StringLen(arr) - 2);
    string token = "";
    int count = 0;
    for(int i = 0; i <= StringLen(inner); i++)
    {
        string ch = (i < StringLen(inner)) ? StringSubstr(inner, i, 1) : ",";
        if(ch == ",")
        {
            StringTrimLeft(token);
            StringTrimRight(token);
            if(StringLen(token) > 0)
            {
                ArrayResize(tps, count + 1);
                tps[count] = (token == "null") ? -1.0 : StringToDouble(token);
                count++;
            }
            token = "";
        }
        else token += ch;
    }
    return count;
}

//+------------------------------------------------------------------+
//| Symbol resolution                                                |
//+------------------------------------------------------------------+
string ResolveSymbol(const string base)
{
    if(SymbolInfoDouble(base, SYMBOL_ASK) > 0) return base;
    string tries[] = {"m", ".", "_micro"};
    for(int i = 0; i < ArraySize(tries); i++)
    {
        string c = base + tries[i];
        if(SymbolInfoDouble(c, SYMBOL_ASK) > 0)
        { Print("Symbol resolved: ", base, " → ", c); return c; }
    }
    int total = SymbolsTotal(false);
    for(int i = 0; i < total; i++)
    {
        string s = SymbolName(i, false);
        if(StringFind(s, base) == 0)
        { Print("Symbol resolved by scan: ", base, " → ", s); return s; }
    }
    Print("WARNING: symbol not found — using as-is: ", base);
    return base;
}

//+------------------------------------------------------------------+
//| Lot size based on RiskPercent of balance                         |
//| lot = (balance * risk%) / (sl_distance / tick_size * tick_value) |
//+------------------------------------------------------------------+
double CalcLot(const string symbol, double sl_distance)
{
    double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
    double risk_amt   = balance * RiskPercent / 100.0;

    double tick_val   = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
    double tick_size  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
    double min_lot    = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
    double max_lot    = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
    double lot_step   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

    if(tick_val == 0 || tick_size == 0 || sl_distance == 0)
    {
        Print("CalcLot: invalid params — using min lot");
        return min_lot;
    }

    double loss_per_lot = (sl_distance / tick_size) * tick_val;
    double lot = risk_amt / loss_per_lot;

    lot = MathFloor(lot / lot_step) * lot_step;
    lot = MathMax(lot, min_lot);
    lot = MathMin(lot, max_lot);

    Print("CalcLot: balance=", balance, " risk=", RiskPercent, "% → $", risk_amt,
          " | sl_dist=", sl_distance, " | loss_per_lot=", loss_per_lot, " → lot=", lot);
    return NormalizeDouble(lot, 2);
}

//+------------------------------------------------------------------+
//| ACTION: open — single trade at RiskPercent, TP at ProfitPercent |
//+------------------------------------------------------------------+
void HandleOpen(const string &json)
{
    string rawSymbol = JsonGetString(json, "symbol");
    string direction = JsonGetString(json, "direction");
    double sl_val    = JsonGetDouble(json, "sl");
    double sl_pts    = JsonGetDouble(json, "sl_points");

    string symbol = ResolveSymbol(rawSymbol);
    SymbolSelect(symbol, true);

    ENUM_ORDER_TYPE orderType = (direction == "buy") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
    int    digits    = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
    double point     = SymbolInfoDouble(symbol, SYMBOL_POINT);
    double tick_val  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
    double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
    double ask       = SymbolInfoDouble(symbol, SYMBOL_ASK);
    double bid       = SymbolInfoDouble(symbol, SYMBOL_BID);
    double entry     = (orderType == ORDER_TYPE_BUY) ? ask : bid;
    double balance   = AccountInfoDouble(ACCOUNT_BALANCE);
    double sign      = (orderType == ORDER_TYPE_BUY) ? 1.0 : -1.0;

    // --- SL Price ---
    double sl_price = 0.0;
    if(sl_val > -999998)
    {
        sl_price = sl_val;
    }
    else if(sl_pts > -999998)
    {
        sl_price = NormalizeDouble(entry - sign * sl_pts * point, digits);
    }
    else
    {
        // No SL — derive from RiskPercent so that risk_amt is lost if SL hit at 1 lot
        double risk_amt   = balance * RiskPercent / 100.0;
        double loss_ratio = (tick_val > 0 && tick_size > 0) ? (tick_val / tick_size) : 0;
        double sl_dist    = (loss_ratio > 0) ? (risk_amt / loss_ratio) : (entry * 0.02);
        sl_price = NormalizeDouble(entry - sign * sl_dist, digits);
        Print("Auto SL: risk_amt=$", risk_amt, " sl_dist=", sl_dist, " sl_price=", sl_price);
    }

    double sl_distance = MathAbs(entry - sl_price);
    double lot = CalcLot(symbol, sl_distance);

    // --- TP: calculated from ProfitPercent of balance (ignores signal TP) ---
    // profit_target = balance * ProfitPercent%
    // tp_distance   = profit_target / lot / (tick_value / tick_size)
    double profit_amt = balance * ProfitPercent / 100.0;
    double tp_distance = 0.0;
    if(lot > 0 && tick_val > 0 && tick_size > 0)
        tp_distance = profit_amt / lot / (tick_val / tick_size);
    double tp = NormalizeDouble(entry + sign * tp_distance, digits);

    trade.SetExpertMagicNumber(MagicNumber);
    trade.SetDeviationInPoints(Deviation);
    trade.SetTypeFilling(ORDER_FILLING_IOC);

    Print("COMPOUND OPEN ", direction, " ", symbol,
          " | lot=", lot,
          " | sl=", sl_price, " (risk $", NormalizeDouble(balance * RiskPercent / 100.0, 2), ")",
          " | tp=", tp, " (target $", NormalizeDouble(profit_amt, 2), ")");

    bool ok;
    if(orderType == ORDER_TYPE_BUY)
        ok = trade.Buy(lot, symbol, 0, sl_price, tp, "TG_COMPOUND");
    else
        ok = trade.Sell(lot, symbol, 0, sl_price, tp, "TG_COMPOUND");

    if(ok)
        Print("  Opened | ticket=", trade.ResultOrder());
    else
        Print("  FAILED | retcode=", trade.ResultRetcode(), " | ", trade.ResultRetcodeDescription());
}

//+------------------------------------------------------------------+
//| ACTION: update — update SL only, keep balance-based TP          |
//+------------------------------------------------------------------+
void HandleUpdate(const string &json)
{
    string rawSymbol = JsonGetString(json, "symbol");
    string direction = JsonGetString(json, "direction");
    double new_sl    = JsonGetDouble(json, "new_sl");

    string symbol = ResolveSymbol(rawSymbol);
    Print("COMPOUND UPDATE ", direction, " ", symbol, " | new_sl=", new_sl, " (keeping balance TP)");

    int updated = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;

        string pos_symbol = PositionGetString(POSITION_SYMBOL);
        long   pos_magic  = PositionGetInteger(POSITION_MAGIC);
        int    pos_type   = (int)PositionGetInteger(POSITION_TYPE);
        string pos_dir    = (pos_type == POSITION_TYPE_BUY) ? "buy" : "sell";

        if(StringFind(pos_symbol, rawSymbol) < 0 && StringFind(rawSymbol, pos_symbol) < 0) continue;
        if(pos_dir != direction) continue;
        if(pos_magic != MagicNumber) continue;

        // Keep the TP that was set at open (30% of balance) — do not change it
        double current_tp = PositionGetDouble(POSITION_TP);

        if(trade.PositionModify(ticket, new_sl, current_tp))
            Print("  Updated ticket=", ticket, " sl=", new_sl, " tp=", current_tp, " (kept)");
        else
            Print("  FAILED ticket=", ticket, " retcode=", trade.ResultRetcode());
        updated++;
    }
    if(updated == 0) Print("UPDATE: no matching compound trades found");
}

//+------------------------------------------------------------------+
//| ACTION: update_sl — SL only, keep TP                            |
//+------------------------------------------------------------------+
void HandleUpdateSL(const string &json)
{
    string rawSymbol = JsonGetString(json, "symbol");
    string direction = JsonGetString(json, "direction");
    double new_sl    = JsonGetDouble(json, "new_sl");

    string symbol = ResolveSymbol(rawSymbol);
    Print("COMPOUND UPDATE_SL ", direction, " ", symbol, " | new_sl=", new_sl);

    int updated = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;

        string pos_symbol = PositionGetString(POSITION_SYMBOL);
        long   pos_magic  = PositionGetInteger(POSITION_MAGIC);
        int    pos_type   = (int)PositionGetInteger(POSITION_TYPE);
        string pos_dir    = (pos_type == POSITION_TYPE_BUY) ? "buy" : "sell";

        if(StringFind(pos_symbol, rawSymbol) < 0 && StringFind(rawSymbol, pos_symbol) < 0) continue;
        if(pos_dir != direction) continue;
        if(pos_magic != MagicNumber) continue;

        double current_tp = PositionGetDouble(POSITION_TP);
        if(trade.PositionModify(ticket, new_sl, current_tp))
            Print("  SL updated ticket=", ticket, " new_sl=", new_sl, " tp=", current_tp, " (kept)");
        else
            Print("  FAILED ticket=", ticket, " retcode=", trade.ResultRetcode());
        updated++;
    }
    if(updated == 0) Print("UPDATE_SL: no matching compound trades found");
}

//+------------------------------------------------------------------+
//| ACTION: breakeven                                                |
//+------------------------------------------------------------------+
void HandleBreakeven(const string &json)
{
    Print("COMPOUND BREAKEVEN — moving to entry + spread buffer");

    int moved = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;
        if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

        string pos_symbol = PositionGetString(POSITION_SYMBOL);
        double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
        double current_sl = PositionGetDouble(POSITION_SL);
        double current_tp = PositionGetDouble(POSITION_TP);
        int    pos_type   = (int)PositionGetInteger(POSITION_TYPE);

        long   spread = SymbolInfoInteger(pos_symbol, SYMBOL_SPREAD);
        double point  = SymbolInfoDouble(pos_symbol, SYMBOL_POINT);
        double buffer = spread * point * 1.5;
        int    digits = (int)SymbolInfoInteger(pos_symbol, SYMBOL_DIGITS);

        double new_sl;
        double bid = SymbolInfoDouble(pos_symbol, SYMBOL_BID);
        double ask = SymbolInfoDouble(pos_symbol, SYMBOL_ASK);

        if(pos_type == POSITION_TYPE_BUY)
        {
            new_sl = NormalizeDouble(open_price + buffer, digits);
            if(new_sl <= current_sl && current_sl > 0) continue;
            if(bid < new_sl)
            {
                Print("  In drawdown — closing ticket=", ticket);
                trade.PositionClose(ticket);
                moved++; continue;
            }
        }
        else
        {
            new_sl = NormalizeDouble(open_price - buffer, digits);
            if(new_sl >= current_sl && current_sl > 0) continue;
            if(ask > new_sl)
            {
                Print("  In drawdown — closing ticket=", ticket);
                trade.PositionClose(ticket);
                moved++; continue;
            }
        }

        if(trade.PositionModify(ticket, new_sl, current_tp))
            Print("  Breakeven set | ticket=", ticket, " new_sl=", new_sl);
        else
            Print("  FAILED ticket=", ticket, " retcode=", trade.ResultRetcode());
        moved++;
    }
    if(moved == 0) Print("BREAKEVEN: no eligible compound trades");
}

//+------------------------------------------------------------------+
//| ACTION: close                                                    |
//+------------------------------------------------------------------+
void HandleClose(const string &json)
{
    Print("COMPOUND CLOSE — closing all compound trades");

    int closed = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;
        if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

        if(trade.PositionClose(ticket))
            Print("  Closed | ticket=", ticket);
        else
            Print("  FAILED | ticket=", ticket, " retcode=", trade.ResultRetcode());
        closed++;
    }
    if(closed == 0) Print("CLOSE: no compound trades found");
}

//+------------------------------------------------------------------+
//| Process a single JSON file                                       |
//+------------------------------------------------------------------+
void ProcessFile(const string filename)
{
    string folder = SignalFolder + "\\";
    int handle = FileOpen(folder + filename, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
    if(handle == INVALID_HANDLE)
    {
        Print("ERROR opening file: ", filename, " err=", GetLastError());
        return;
    }
    string json = "";
    while(!FileIsEnding(handle)) json += FileReadString(handle);
    FileClose(handle);

    string action = JsonGetString(json, "action");
    Print("--- [Compound] Processing: ", filename, " | action=", action, " ---");

    if(action == "open")           HandleOpen(json);
    else if(action == "update")    HandleUpdate(json);
    else if(action == "update_sl") HandleUpdateSL(json);
    else if(action == "breakeven") HandleBreakeven(json);
    else if(action == "close")     HandleClose(json);
    else Print("Unknown action: ", action);

    FileDelete(folder + filename, FILE_COMMON);
}

//+------------------------------------------------------------------+
int OnInit()
{
    EventSetMillisecondTimer(CheckIntervalMs);
    Print("TelegramCompoundEA v1.0 started | folder: Common\\Files\\", SignalFolder,
          " | magic=", MagicNumber, " | risk=", RiskPercent, "%");
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
{
    string folder = SignalFolder + "\\";
    string filename;
    long h = FileFindFirst(folder + "*.json", filename, FILE_COMMON);
    if(h == INVALID_HANDLE) return;
    do {
        if(StringFind(filename, ".json") >= 0) ProcessFile(filename);
    } while(FileFindNext(h, filename));
    FileFindClose(h);
}

void OnTick() {}
//+------------------------------------------------------------------+
