//+------------------------------------------------------------------+
//|  TelegramSignalEA.mq5  v2.5                                      |
//|  Handles: open / update / breakeven actions from Python bot.     |
//|  Reads from MT5 Common Files folder every second.                |
//+------------------------------------------------------------------+
#property copyright "MyTradeBot"
#property version   "2.50"
#property strict

#include <Trade\Trade.mqh>

CTrade trade;

input int    CheckIntervalMs = 1000;
input string SignalFolder    = "signals";
input int    MagicBase       = 234000;  // must match MAGIC_NUMBER in .env
input double AutoBE_Trigger  = 0.0;    // 0 = disabled; channel message triggers BE instead
input double BE_Offset       = 2.5;    // distance past entry when moving SL to BE (clears broker min-stop-distance)
input double AutoSL_Distance = 15.0;   // SL distance from entry when the signal has no SL yet ("Sell now"); also the lot-sizing distance
input int    MaxOpenAgeSec   = 120;    // skip open signals older than this (e.g. MT5 was closed when they arrived)

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
    string tries[] = {"c", "m", ".", ".s", "_micro"};
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

    // Refresh market data to ensure tick values are loaded
    SymbolSelect(symbol, true);
    double tick_val   = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
    double tick_size  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
    double min_lot    = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
    double max_lot    = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
    double lot_step   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

    // If tick_val still 0, retry once after a short wait
    if(tick_val == 0)
    {
        Sleep(500);
        tick_val = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
        tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
    }

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
//+------------------------------------------------------------------+
//| Signal entry per trade — the channel's entry price, kept in a     |
//| terminal global variable so it survives EA/terminal restarts.     |
//| Breakeven moves SL to this level instead of our own fill price.   |
//+------------------------------------------------------------------+
string EntryVarName(const ulong ticket) { return "TGE_" + IntegerToString((long)ticket); }

void SetSignalEntry(const ulong ticket, const double price)
{
    if(price > 0) GlobalVariableSet(EntryVarName(ticket), price);
}

double GetSignalEntry(const ulong ticket)
{
    string name = EntryVarName(ticket);
    return GlobalVariableCheck(name) ? GlobalVariableGet(name) : 0.0;
}

// Trades are commented "TG_<signal_id>_<n>" so follow-up SL updates touch only their own signal
string GroupPrefix(const string sid) { return "TG_" + StringSubstr(sid, 0, 16) + "_"; }

// Targeted update: the signal_id names a real signal (not a generic "sl_move"/"edit"/"sl_update")
bool IsTargetedSignal(const string sid)
{
    return sid != "" && sid != "sl_update" && sid != "sl_move" && sid != "edit";
}

// Number of open positions carrying this signal's comment prefix
int CountGroup(const string sid, const int magic_base)
{
    int n = 0;
    string prefix = GroupPrefix(sid);
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;
        long pos_magic = PositionGetInteger(POSITION_MAGIC);
        if(pos_magic < magic_base || pos_magic > magic_base + 200) continue;
        if(StringFind(PositionGetString(POSITION_COMMENT), prefix) == 0) n++;
    }
    return n;
}

// Remove entry records of trades that are no longer open
void CleanupSignalEntries()
{
    for(int i = GlobalVariablesTotal() - 1; i >= 0; i--)
    {
        string name = GlobalVariableName(i);
        if(StringFind(name, "TGE_") != 0) continue;
        ulong ticket = (ulong)StringToInteger(StringSubstr(name, 4));
        if(!PositionSelectByTicket(ticket)) GlobalVariableDel(name);
    }
}

void HandleOpen(const string &json)
{
    string rawSymbol = JsonGetString(json, "symbol");
    string direction = JsonGetString(json, "direction");
    double sl_val    = JsonGetDouble(json, "sl");        // -999999 = null
    double sl_pts    = JsonGetDouble(json, "sl_points"); // -999999 = null
    int    magic     = (int)JsonGetDouble(json, "magic");

    // Block duplicate opens unless force=true (force allows new trades alongside existing ones)
    // JSON booleans are not quoted, so check raw content for "true"
    bool forceOpen = (StringFind(json, "\"force\": true") >= 0 ||
                      StringFind(json, "\"force\":true")  >= 0);
    string checkSymbol = ResolveSymbol(rawSymbol);
    if(!forceOpen)
    {
        ENUM_POSITION_TYPE checkType = (direction == "buy") ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;
        for(int c = PositionsTotal() - 1; c >= 0; c--)
        {
            if(!PositionGetTicket(c)) continue;
            long   pos_magic = PositionGetInteger(POSITION_MAGIC);
            string pos_sym   = PositionGetString(POSITION_SYMBOL);
            int    pos_type  = (int)PositionGetInteger(POSITION_TYPE);
            if(pos_magic >= magic && pos_magic <= magic + 200 &&
               StringFind(pos_sym, checkSymbol) >= 0 &&
               pos_type == (int)checkType)
            {
                Print("OPEN skipped — trades already open for ", direction, " ", checkSymbol);
                return;
            }
        }
    }
    int    deviation = (int)JsonGetDouble(json, "deviation");
    int    lb_div    = (int)JsonGetDouble(json, "lot_balance_div");
    string tpsRaw    = JsonGetArray(json, "tps");

    double tps[];
    int numTPs = ParseTPs(tpsRaw, tps);
    if(numTPs == 0) { Print("OPEN: no TPs found"); return; }

    double tp_step = JsonGetDouble(json, "tp_step"); // -999999 = not set
    double sig_entry = JsonGetDouble(json, "entry_price"); // -999999 = not in signal
    string sid       = JsonGetString(json, "signal_id");

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
        // No SL provided — use fixed AutoSL_Distance from entry.
        sl_price = (orderType == ORDER_TYPE_BUY)
            ? NormalizeDouble(entry - AutoSL_Distance, digits)
            : NormalizeDouble(entry + AutoSL_Distance, digits);
        Print("Auto SL (fixed ", AutoSL_Distance, "): entry=", entry, " sl_price=", sl_price);
    }

    // Validate SL is on the correct side of entry — fall back to AutoSL_Distance if wrong
    bool sl_valid = (orderType == ORDER_TYPE_BUY)  ? (sl_price < entry)
                                                   : (sl_price > entry);
    if(!sl_valid && sl_price != 0.0)
    {
        double corrected = (orderType == ORDER_TYPE_BUY)
            ? NormalizeDouble(entry - AutoSL_Distance, digits)
            : NormalizeDouble(entry + AutoSL_Distance, digits);
        Print("SL validation failed (sl=", sl_price, " entry=", entry, " dir=", direction,
              ") — using default SL distance ", AutoSL_Distance, ": ", corrected);
        sl_price = corrected;
    }

    // Always size lot on the fixed AutoSL_Distance for consistent risk on all signals
    double lot = CalcLot(symbol, numTPs, lb, AutoSL_Distance);

    trade.SetDeviationInPoints(deviation > 0 ? deviation : 20);
    trade.SetTypeFilling(ORDER_FILLING_IOC);

    Print("OPEN ", direction, " ", symbol, " | lot=", lot, " | ", numTPs, " TPs | sl=",
          sl_price, " entry~=", entry);

    for(int i = 0; i < numTPs; i++)
    {
        trade.SetExpertMagicNumber(magic + i);

        // TP: use provided value, or auto-calculate from entry ± tp_step*i (last trade = open)
        double tp;
        if(tps[i] > 0)
            tp = tps[i];
        else if(tp_step > -999998 && i < numTPs - 1)
            tp = NormalizeDouble(entry + (orderType == ORDER_TYPE_BUY ? 1 : -1) * tp_step * (i + 1), digits);
        else
            tp = 0.0; // last trade or no step: open-ended

        double sl = sl_price;

        bool ok;
        if(orderType == ORDER_TYPE_BUY)
            ok = trade.Buy(lot, symbol, 0, sl, tp, GroupPrefix(sid) + IntegerToString(i + 1));
        else
            ok = trade.Sell(lot, symbol, 0, sl, tp, GroupPrefix(sid) + IntegerToString(i + 1));

        if(ok) SetSignalEntry(trade.ResultOrder(), sig_entry);
        if(ok)
            Print("  TP", i+1, " opened | ticket=", trade.ResultOrder(),
                  " | sl=", sl, " | tp=", (tp > 0 ? DoubleToString(tp,5) : "OPEN"));
        else
            Print("  TP", i+1, " FAILED | retcode=", trade.ResultRetcode(),
                  " | ", trade.ResultRetcodeDescription());
    }
}

//+------------------------------------------------------------------+
//| ACTION: update — set the signal's SL and TPs on existing trades  |
//+------------------------------------------------------------------+
void HandleUpdate(const string &json)
{
    string rawSymbol = JsonGetString(json, "symbol");
    string direction = JsonGetString(json, "direction");
    double new_sl    = JsonGetDouble(json, "new_sl");
    int    magic_base= (int)JsonGetDouble(json, "magic");
    string tpsRaw    = JsonGetArray(json, "tps");
    double sig_entry = JsonGetDouble(json, "entry_price"); // -999999 = not in signal
    string sid       = JsonGetString(json, "signal_id");
    // Only this signal's trades — unless none carry its comment (e.g. broker dropped it)
    bool   targeted  = IsTargetedSignal(sid) && CountGroup(sid, magic_base) > 0;

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
        if(targeted && StringFind(PositionGetString(POSITION_COMMENT), GroupPrefix(sid)) != 0) continue;

        int    tp_index   = (int)(pos_magic - magic_base);
        double current_sl = PositionGetDouble(POSITION_SL);
        double current_tp = PositionGetDouble(POSITION_TP);
        bool   is_buy     = (pos_type == POSITION_TYPE_BUY);
        int    digits     = (int)SymbolInfoInteger(pos_symbol, SYMBOL_DIGITS);
        SetSignalEntry(ticket, sig_entry);
        updated++;

        // TP per slot: last slot always open; slots the signal gives a number for take it;
        // slots it leaves blank keep their current (auto) TP
        double tp = current_tp;
        if(tp_index == numTPs - 1)                     tp = 0.0;
        else if(tp_index < numTPs && tps[tp_index] > 0) tp = NormalizeDouble(tps[tp_index], digits);
        double sl = (new_sl > 0) ? NormalizeDouble(new_sl, digits) : current_sl;

        // Stops must sit on the correct side of current price (plus broker minimum distance).
        // If price is already past the signal's SL or TP, that level has effectively been hit — close.
        double min_dist = SymbolInfoInteger(pos_symbol, SYMBOL_TRADE_STOPS_LEVEL)
                          * SymbolInfoDouble(pos_symbol, SYMBOL_POINT);
        double bid = SymbolInfoDouble(pos_symbol, SYMBOL_BID);
        double ask = SymbolInfoDouble(pos_symbol, SYMBOL_ASK);
        bool sl_ok = (sl <= 0) || (is_buy ? sl < bid - min_dist : sl > ask + min_dist);
        bool tp_ok = (tp <= 0) || (is_buy ? tp > bid + min_dist : tp < ask - min_dist);

        if(!sl_ok || !tp_ok)
        {
            Print("  Price ", (is_buy ? bid : ask), " already past signal ", (!sl_ok ? "SL " : "TP "),
                  (!sl_ok ? sl : tp), " — closing ticket=", ticket);
            if(trade.PositionClose(ticket))
                Print("  Closed ticket=", ticket);
            else
                Print("  Close FAILED ticket=", ticket, " retcode=", trade.ResultRetcode(),
                      " | ", trade.ResultRetcodeDescription());
            continue;
        }

        if(trade.PositionModify(ticket, sl, tp))
            Print("  Updated ticket=", ticket, " TP", tp_index + 1, " sl=", sl,
                  " tp=", (tp > 0 ? DoubleToString(tp, digits) : "OPEN"));
        else
            Print("  Update FAILED ticket=", ticket, " retcode=", trade.ResultRetcode(),
                  " | ", trade.ResultRetcodeDescription());
    }
    if(updated == 0) Print("UPDATE: no matching trades found for ", symbol, " ", direction);
}

//+------------------------------------------------------------------+
//| ACTION: update_sl — update SL only, keep existing TPs           |
//+------------------------------------------------------------------+
void HandleUpdateSL(const string &json)
{
    string rawSymbol = JsonGetString(json, "symbol");
    string direction = JsonGetString(json, "direction");
    double new_sl    = JsonGetDouble(json, "new_sl");
    int    magic_base= (int)JsonGetDouble(json, "magic");
    double sig_entry = JsonGetDouble(json, "entry_price"); // -999999 = not in signal
    string sid       = JsonGetString(json, "signal_id");
    // Only this signal's trades — unless none carry its comment (e.g. broker dropped it)
    bool   targeted  = IsTargetedSignal(sid) && CountGroup(sid, magic_base) > 0;

    string symbol = ResolveSymbol(rawSymbol);

    Print("UPDATE_SL ", direction, " ", symbol, " | new_sl=", new_sl, " (keeping auto TPs)");

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
        if(pos_magic < magic_base || pos_magic > magic_base + 200) continue;
        if(targeted && StringFind(PositionGetString(POSITION_COMMENT), GroupPrefix(sid)) != 0) continue;

        double current_tp = PositionGetDouble(POSITION_TP); // keep unchanged
        SetSignalEntry(ticket, sig_entry);

        if(trade.PositionModify(ticket, new_sl, current_tp))
            Print("  SL updated ticket=", ticket, " new_sl=", new_sl, " tp=", current_tp, " (unchanged)");
        else
            Print("  SL update FAILED ticket=", ticket, " retcode=", trade.ResultRetcode());
        updated++;
    }
    if(updated == 0) Print("UPDATE_SL: no matching trades found for ", symbol, " ", direction);
}

//+------------------------------------------------------------------+
//| ACTION: breakeven — move SL to entry + spread buffer            |
//+------------------------------------------------------------------+
// "Move SL to BE after first TP" — arm, and let OnTimer move SL once the TP1 trade has closed
string ARM_VAR = "TGE_ARM_BE_TP1";

int HandleBreakeven(const string &json)
{
    int magic_base = (int)JsonGetDouble(json, "magic");
    bool afterTP1 = (StringFind(json, "\"after_tp1\": true") >= 0 ||
                     StringFind(json, "\"after_tp1\":true")  >= 0);
    if(afterTP1)
    {
        GlobalVariableSet(ARM_VAR, magic_base);
        Print("BREAKEVEN armed — will move SL to signal entry once TP1 closes");
        return 1;
    }
    return MoveAllToBE(magic_base);
}

// Armed BE fires when bot trades remain but the TP1 slot (magic_base + 0) is gone
void CheckArmedBreakeven()
{
    if(!GlobalVariableCheck(ARM_VAR)) return;
    int magic_base = (int)GlobalVariableGet(ARM_VAR);
    int remaining = 0;
    bool tp1_open = false;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;
        long pos_magic = PositionGetInteger(POSITION_MAGIC);
        if(pos_magic < magic_base || pos_magic > magic_base + 200) continue;
        remaining++;
        if(pos_magic == magic_base) tp1_open = true;
    }
    if(remaining == 0)
    {
        GlobalVariableDel(ARM_VAR);
        Print("BREAKEVEN (after TP1) disarmed — no bot trades left");
    }
    else if(!tp1_open)
    {
        GlobalVariableDel(ARM_VAR);
        Print("TP1 closed — moving remaining trades to BE");
        MoveAllToBE(magic_base);
    }
}

int MoveAllToBE(const int magic_base)
{
    Print("BREAKEVEN — SL to signal entry (fallback: own entry + buffer); close if price is already past it");

    int moved = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;

        long pos_magic = PositionGetInteger(POSITION_MAGIC);
        if(pos_magic < magic_base || pos_magic > magic_base + 9999) continue;
        moved++;

        string pos_symbol = PositionGetString(POSITION_SYMBOL);
        double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
        double current_sl = PositionGetDouble(POSITION_SL);
        double current_tp = PositionGetDouble(POSITION_TP);
        int    pos_type   = (int)PositionGetInteger(POSITION_TYPE);
        int    digits     = (int)SymbolInfoInteger(pos_symbol, SYMBOL_DIGITS);
        bool   is_buy     = (pos_type == POSITION_TYPE_BUY);

        // BE level: the channel's signal entry; without one, own entry ± offset
        // (exact own entry gets rejected as "invalid stops" right at the fill)
        double sig_entry = GetSignalEntry(ticket);
        double be_sl = (sig_entry > 0)
            ? NormalizeDouble(sig_entry, digits)
            : NormalizeDouble(is_buy ? open_price - BE_Offset : open_price + BE_Offset, digits);
        string be_src = (sig_entry > 0) ? "signal entry" : "own entry";

        // SL already at or beyond BE (locked profit) — never loosen it
        if(current_sl > 0 && (is_buy ? current_sl >= be_sl : current_sl <= be_sl))
        {
            Print("  BE skip | ticket=", ticket, " sl=", current_sl, " already at/beyond ", be_src, " ", be_sl);
            continue;
        }

        // The SL can only sit on the profit side of current price (plus the broker's minimum distance).
        // If price is already past the BE level, the trade is in loss vs. BE — close it at market.
        double min_dist = SymbolInfoInteger(pos_symbol, SYMBOL_TRADE_STOPS_LEVEL)
                          * SymbolInfoDouble(pos_symbol, SYMBOL_POINT);
        double bid = SymbolInfoDouble(pos_symbol, SYMBOL_BID);
        double ask = SymbolInfoDouble(pos_symbol, SYMBOL_ASK);
        bool   can_set = is_buy ? (be_sl < bid - min_dist) : (be_sl > ask + min_dist);

        if(can_set && trade.PositionModify(ticket, be_sl, current_tp))
        {
            Print("  BE set | ticket=", ticket, " ", be_src, " | fill=", open_price,
                  " signal=", (sig_entry > 0 ? DoubleToString(sig_entry, digits) : "n/a"), " new_sl=", be_sl);
            continue;
        }

        if(can_set)
            Print("  BE modify FAILED | ticket=", ticket, " retcode=", trade.ResultRetcode(), " — closing instead");
        else
            Print("  BE not possible | ticket=", ticket, " price ", (is_buy ? bid : ask), " is past ", be_src,
                  " ", be_sl, " — closing trade");

        if(trade.PositionClose(ticket))
            Print("  BE close | ticket=", ticket, " closed at market");
        else
            Print("  BE close FAILED | ticket=", ticket, " retcode=", trade.ResultRetcode(),
                  " | ", trade.ResultRetcodeDescription());
    }
    if(moved == 0) Print("BREAKEVEN: no eligible trades found");
    return moved;
}

//+------------------------------------------------------------------+
//| ACTION: close — close all bot trades                             |
//+------------------------------------------------------------------+
int HandleClose(const string &json)
{
    int magic_base = (int)JsonGetDouble(json, "magic");

    Print("CLOSE — closing all bot trades (magic_base=", magic_base, " total_positions=", PositionsTotal(), ")");

    int closed = 0;
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;

        long pos_magic = PositionGetInteger(POSITION_MAGIC);
        string pos_sym = PositionGetString(POSITION_SYMBOL);
        Print("  Checking position: ticket=", ticket, " magic=", pos_magic, " symbol=", pos_sym);

        if(pos_magic < magic_base || pos_magic > magic_base + 9999) continue;

        if(trade.PositionClose(ticket))
            Print("  Closed | ticket=", ticket);
        else
            Print("  Close FAILED | ticket=", ticket, " retcode=", trade.ResultRetcode(),
                  " | ", trade.ResultRetcodeDescription());
        closed++;
    }
    if(closed == 0) Print("CLOSE: no bot trades found in range ", magic_base, "-", magic_base+9999);
    else Print("CLOSE: closed ", closed, " trades");
    return closed;
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

    bool deleteFile = true;

    // Python writes UTC epoch timestamps — compare against GMT, not broker server time
    double age = (double)(TimeGMT() - (datetime)JsonGetDouble(json, "timestamp"));

    if(action == "open" && age > MaxOpenAgeSec)
        Print("OPEN skipped — signal is ", (int)age, "s old (max ", MaxOpenAgeSec, "s)");
    else if(action == "open")      HandleOpen(json);
    else if(action == "update")    HandleUpdate(json);
    else if(action == "update_sl") HandleUpdateSL(json);
    else if(action == "breakeven")
    {
        // Retry breakeven if no trades found yet (trades may still be opening)
        int moved = HandleBreakeven(json);
        if(moved == 0)
        {
            if(age < 30) // retry for up to 30 seconds
            {
                Print("BREAKEVEN: no trades yet, will retry (age=", (int)age, "s)");
                deleteFile = false;
            }
            else
                Print("BREAKEVEN: gave up after 30s, no trades found");
        }
    }
    else if(action == "close")
    {
        // Retry close if no trades found yet (trades may still be opening)
        int closed = HandleClose(json);
        if(closed == 0)
        {
            if(age < 30) // retry for up to 30 seconds
            {
                Print("CLOSE: no trades yet, will retry (age=", (int)age, "s)");
                deleteFile = false;
            }
            else
                Print("CLOSE: gave up after 30s, no trades found");
        }
    }
    else Print("Unknown action: ", action);

    if(deleteFile) FileDelete(folder + filename, FILE_COMMON);
}

//+------------------------------------------------------------------+
//| Auto breakeven — runs every timer tick                           |
//| BUY:  when bid >= entry + AutoBE_Trigger → SL = entry           |
//| SELL: when ask <= entry - AutoBE_Trigger → SL = entry           |
//+------------------------------------------------------------------+
void AutoBreakeven()
{
    if(AutoBE_Trigger <= 0) return;

    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(!PositionSelectByTicket(ticket)) continue;

        long pos_magic = PositionGetInteger(POSITION_MAGIC);
        if(pos_magic < MagicBase || pos_magic > MagicBase + 9999) continue;

        string pos_symbol = PositionGetString(POSITION_SYMBOL);
        int    pos_type   = (int)PositionGetInteger(POSITION_TYPE);
        double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
        double current_sl = PositionGetDouble(POSITION_SL);
        double current_tp = PositionGetDouble(POSITION_TP);
        int    digits     = (int)SymbolInfoInteger(pos_symbol, SYMBOL_DIGITS);

        double be_sl = (pos_type == POSITION_TYPE_BUY)
            ? NormalizeDouble(open_price - BE_Offset, digits)
            : NormalizeDouble(open_price + BE_Offset, digits);

        if(pos_type == POSITION_TYPE_BUY)
        {
            // Already at or past breakeven
            if(current_sl >= be_sl && current_sl != 0.0) continue;
            double bid = SymbolInfoDouble(pos_symbol, SYMBOL_BID);
            if(bid >= open_price + AutoBE_Trigger)
            {
                if(trade.PositionModify(ticket, be_sl, current_tp))
                    Print("AutoBE BUY | ticket=", ticket, " entry=", open_price,
                          " bid=", bid, " → sl=", be_sl);
            }
        }
        else
        {
            // Already at or past breakeven
            if(current_sl != 0.0 && current_sl <= be_sl) continue;
            double ask = SymbolInfoDouble(pos_symbol, SYMBOL_ASK);
            if(ask <= open_price - AutoBE_Trigger)
            {
                if(trade.PositionModify(ticket, be_sl, current_tp))
                    Print("AutoBE SELL | ticket=", ticket, " entry=", open_price,
                          " ask=", ask, " → sl=", be_sl);
            }
        }
    }
}

//+------------------------------------------------------------------+
int OnInit()
{
    EventSetMillisecondTimer(CheckIntervalMs);
    CleanupSignalEntries();
    Print("TelegramSignalEA v2.0 started | folder: Common\\Files\\", SignalFolder);
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
{
    AutoBreakeven();
    CheckArmedBreakeven();

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
