//+------------------------------------------------------------------+
//|  TelegramSignalEA.mq5  v2.2                                      |
//|  Handles: open / update / breakeven actions from Python bot.     |
//|  Reads from MT5 Common Files folder every second.                |
//+------------------------------------------------------------------+
#property copyright "MyTradeBot"
#property version   "2.20"
#property strict

#include <Trade\Trade.mqh>

CTrade trade;

input int    CheckIntervalMs = 1000;
input string SignalFolder    = "signals";

//+------------------------------------------------------------------+
//| Minimal JSON helpers                                             |
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
    if(pos < 0) return -999999.0;  // sentinel: key not found
    pos = StringFind(json, ":", pos);
    if(pos < 0) return -999999.0;
    pos++;
    while(pos < StringLen(json) && StringSubstr(json, pos, 1) == " ") pos++;
    // Check for null
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
//| Symbol resolution — tries base name, then common broker suffixes |
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
//| Lot size based on risk                                           |
//| risk_per_trade = balance * 25% / num_tps                        |
//| lot = risk_per_trade / (sl_distance / tick_size * tick_value)   |
//+------------------------------------------------------------------+
double CalcLot(const string symbol, int num_tps, int lot_balance_div, double sl_distance)
{
    double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
    double risk       = (balance / lot_balance_div) / num_tps;

    double tick_val   = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
    double tick_size  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
    double min_lot    = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
    double max_lot    = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
    double lot_step   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

    if(tick_val == 0 || tick_size == 0 || sl_distance == 0)
    {
        Print("CalcLot: cannot calculate — using min lot. tick_val=", tick_val,
              " tick_size=", tick_size, " sl_dist=", sl_distance);
        return min_lot;
    }

    // ticks in SL distance × tick value per lot = $ loss per lot
    double loss_per_lot = (sl_distance / tick_size) * tick_val;
    double lot = risk / loss_per_lot;

    lot = MathFloor(lot / lot_step) * lot_step;
    lot = MathMax(lot, min_lot);
    lot = MathMin(lot, max_lot);

    Print("CalcLot: balance=", balance, " risk=", risk, " sl_dist=", sl_distance,
          " loss_per_lot=", loss_per_lot, " → lot=", lot);
    return NormalizeDouble(lot, 2);
}

//+------------------------------------------------------------------+
//| ACTION: open                                                     |
//+------------------------------------------------------------------+
void HandleOpen(const string &json)
{
    string rawSymbol = JsonGetString(json, "symbol");
    string direction = JsonGetString(json, "direction");
    double sl_val    = JsonGetDouble(json, "sl");        // -999999 = null
    double sl_pts    = JsonGetDouble(json, "sl_points"); // -999999 = null
    int    magic     = (int)JsonGetDouble(json, "magic");
    int    deviation = (int)JsonGetDouble(json, "deviation");
    int    lb_div    = (int)JsonGetDouble(json, "lot_balance_div");
    string tpsRaw    = JsonGetArray(json, "tps");

    double tps[];
    int numTPs = ParseTPs(tpsRaw, tps);
    if(numTPs == 0) { Print("OPEN: no TPs found"); return; }

    string symbol = ResolveSymbol(rawSymbol);
    SymbolSelect(symbol, true);

    ENUM_ORDER_TYPE orderType = (direction == "buy") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
    double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
    double ask   = SymbolInfoDouble(symbol, SYMBOL_ASK);
    double bid   = SymbolInfoDouble(symbol, SYMBOL_BID);
    double entry = (orderType == ORDER_TYPE_BUY) ? ask : bid;

    // Calculate SL price first so we can use it for lot sizing
    int    digits    = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
    double tick_val  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
    double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
    int    lb        = lb_div > 0 ? lb_div : 4;

    double sl_price = 0.0;
    if(sl_val > -999998)
    {
        sl_price = sl_val;
    }
    else if(sl_pts > -999998)
    {
        sl_price = (orderType == ORDER_TYPE_BUY)
            ? NormalizeDouble(entry - sl_pts * point, digits)
            : NormalizeDouble(entry + sl_pts * point, digits);
    }
    else
    {
        // No SL provided — derive SL from risk so 1 lot risks exactly risk_per_trade
        double risk_per_trade = AccountInfoDouble(ACCOUNT_BALANCE) / lb / numTPs;
        double loss_ratio     = (tick_val > 0 && tick_size > 0) ? (tick_val / tick_size) : 0;
        double sl_dist_auto   = (loss_ratio > 0) ? (risk_per_trade / loss_ratio) : (entry * 0.02);
        sl_price = (orderType == ORDER_TYPE_BUY)
            ? NormalizeDouble(entry - sl_dist_auto, digits)
            : NormalizeDouble(entry + sl_dist_auto, digits);
        Print("Auto SL: risk_per_trade=", risk_per_trade, " loss_ratio=", loss_ratio,
              " sl_dist=", sl_dist_auto, " sl_price=", sl_price);
    }

    double sl_distance = MathAbs(entry - sl_price);
    double lot = CalcLot(symbol, numTPs, lb, sl_distance);

    trade.SetDeviationInPoints(deviation > 0 ? deviation : 20);
    trade.SetTypeFilling(ORDER_FILLING_IOC);

    Print("OPEN ", direction, " ", symbol, " | lot=", lot, " | ", numTPs, " TPs | sl=",
          sl_price, " entry~=", entry);

    for(int i = 0; i < numTPs; i++)
    {
        trade.SetExpertMagicNumber(magic + i);

        double tp = (tps[i] > 0) ? tps[i] : 0.0;
        double sl = sl_price;

        bool ok;
        if(orderType == ORDER_TYPE_BUY)
            ok = trade.Buy(lot, symbol, 0, sl, tp, StringFormat("TG_TP%d", i + 1));
        else
            ok = trade.Sell(lot, symbol, 0, sl, tp, StringFormat("TG_TP%d", i + 1));

        if(ok)
            Print("  TP", i+1, " opened | ticket=", trade.ResultOrder(),
                  " | sl=", sl, " | tp=", (tp > 0 ? DoubleToString(tp,5) : "OPEN"));
        else
            Print("  TP", i+1, " FAILED | retcode=", trade.ResultRetcode(),
                  " | ", trade.ResultRetcodeDescription());
    }
}

//+------------------------------------------------------------------+
//| ACTION: update — modify SL/TP on existing trades                 |
//+------------------------------------------------------------------+
void HandleUpdate(const string &json)
{
    string rawSymbol = JsonGetString(json, "symbol");
    string direction = JsonGetString(json, "direction");
    double new_sl    = JsonGetDouble(json, "new_sl");
    int    magic_base= (int)JsonGetDouble(json, "magic");
    string tpsRaw    = JsonGetArray(json, "tps");

    double tps[];
    int numTPs = ParseTPs(tpsRaw, tps);

    string symbol = ResolveSymbol(rawSymbol);

    Print("UPDATE ", direction, " ", symbol, " | new_sl=", new_sl, " | ", numTPs, " TPs");

    int updated = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;

        string pos_symbol = PositionGetString(POSITION_SYMBOL);
        long   pos_magic  = PositionGetInteger(POSITION_MAGIC);
        int    pos_type   = (int)PositionGetInteger(POSITION_TYPE);
        string pos_dir    = (pos_type == POSITION_TYPE_BUY) ? "buy" : "sell";

        // Match: same symbol prefix, same direction, magic in our range
        if(StringFind(pos_symbol, rawSymbol) < 0 && StringFind(rawSymbol, pos_symbol) < 0) continue;
        if(pos_dir != direction) continue;
        if(pos_magic < magic_base || pos_magic > magic_base + 200) continue;

        int tp_index = (int)(pos_magic - magic_base);
        double tp = 0.0;
        if(tp_index < numTPs && tps[tp_index] > 0) tp = tps[tp_index];

        if(trade.PositionModify(ticket, new_sl, tp))
            Print("  Updated ticket=", ticket, " sl=", new_sl, " tp=", (tp>0?DoubleToString(tp,5):"OPEN"));
        else
            Print("  Update FAILED ticket=", ticket, " retcode=", trade.ResultRetcode());
        updated++;
    }
    if(updated == 0) Print("UPDATE: no matching trades found for ", symbol, " ", direction);
}

//+------------------------------------------------------------------+
//| ACTION: breakeven — move SL to entry + spread buffer            |
//+------------------------------------------------------------------+
void HandleBreakeven(const string &json)
{
    int magic_base = (int)JsonGetDouble(json, "magic");

    Print("BREAKEVEN — moving all bot trades to entry + spread buffer");

    int moved = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;

        long pos_magic = PositionGetInteger(POSITION_MAGIC);
        if(pos_magic < magic_base || pos_magic > magic_base + 9999) continue;

        string pos_symbol  = PositionGetString(POSITION_SYMBOL);
        double open_price  = PositionGetDouble(POSITION_PRICE_OPEN);
        double current_sl  = PositionGetDouble(POSITION_SL);
        double current_tp  = PositionGetDouble(POSITION_TP);
        int    pos_type    = (int)PositionGetInteger(POSITION_TYPE);

        // Buffer = 1.5× current spread (small, covers spread without big offset)
        long   spread = SymbolInfoInteger(pos_symbol, SYMBOL_SPREAD);
        double point  = SymbolInfoDouble(pos_symbol, SYMBOL_POINT);
        double buffer = spread * point * 1.5;

        double new_sl;
        double current_bid = SymbolInfoDouble(pos_symbol, SYMBOL_BID);
        double current_ask = SymbolInfoDouble(pos_symbol, SYMBOL_ASK);

        if(pos_type == POSITION_TYPE_BUY)
        {
            new_sl = NormalizeDouble(open_price + buffer, (int)SymbolInfoInteger(pos_symbol, SYMBOL_DIGITS));
            // Already past breakeven — skip
            if(new_sl <= current_sl && current_sl > 0) continue;
            // In drawdown — price hasn't reached breakeven level — close instead
            if(current_bid < new_sl)
            {
                Print("  In drawdown — closing ticket=", ticket, " bid=", current_bid, " < be=", new_sl);
                if(trade.PositionClose(ticket))
                    Print("  Closed | ticket=", ticket);
                else
                    Print("  Close FAILED | ticket=", ticket, " retcode=", trade.ResultRetcode());
                moved++;
                continue;
            }
        }
        else
        {
            new_sl = NormalizeDouble(open_price - buffer, (int)SymbolInfoInteger(pos_symbol, SYMBOL_DIGITS));
            if(new_sl >= current_sl && current_sl > 0) continue;
            // In drawdown — close instead
            if(current_ask > new_sl)
            {
                Print("  In drawdown — closing ticket=", ticket, " ask=", current_ask, " > be=", new_sl);
                if(trade.PositionClose(ticket))
                    Print("  Closed | ticket=", ticket);
                else
                    Print("  Close FAILED | ticket=", ticket, " retcode=", trade.ResultRetcode());
                moved++;
                continue;
            }
        }

        if(trade.PositionModify(ticket, new_sl, current_tp))
            Print("  Breakeven set | ticket=", ticket, " entry=", open_price,
                  " new_sl=", new_sl, " buffer=", buffer);
        else
            Print("  Breakeven FAILED | ticket=", ticket, " retcode=", trade.ResultRetcode());
        moved++;
    }
    if(moved == 0) Print("BREAKEVEN: no eligible trades found");
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
    Print("--- Processing file: ", filename, " | action=", action, " ---");

    if(action == "open")           HandleOpen(json);
    else if(action == "update")    HandleUpdate(json);
    else if(action == "breakeven") HandleBreakeven(json);
    else Print("Unknown action: ", action);

    FileDelete(folder + filename, FILE_COMMON);
}

//+------------------------------------------------------------------+
int OnInit()
{
    EventSetMillisecondTimer(CheckIntervalMs);
    Print("TelegramSignalEA v2.0 started | folder: Common\\Files\\", SignalFolder);
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
