// strategy.v -- deliberately toy top-of-book imbalance trigger.
//   both   = bid & ask present and (ask - bid) <= max_spread
//   buy_c  = both && bid_qty >= ask_qty << imb_shift   (bid-heavy: lift the ask)
//   sell_c = both && ask_qty >= bid_qty << imb_shift   (ask-heavy: hit the bid)
// Edge triggered: fires only when a condition goes false -> true across book
// updates (buy has priority).  Order: IOC at the opposite touch for
// min(touch qty, max_qty).  Purely combinational fire -> no added cycle.
module strategy (
    input  wire        clk,
    input  wire        rst,
    input  wire        upd,
    input  wire [31:0] upd_id,
    input  wire [31:0] upd_sof,
    input  wire [31:0] upd_eom,
    input  wire        bid_v,
    input  wire [31:0] bid_px,
    input  wire [31:0] bid_q,
    input  wire        ask_v,
    input  wire [31:0] ask_px,
    input  wire [31:0] ask_q,
    input  wire        cfg_enable,
    input  wire [3:0]  cfg_imb_shift,
    input  wire [31:0] cfg_max_spread,
    input  wire [31:0] cfg_max_qty,
    output wire        fire,
    output wire        fire_side,   // 0 = buy, 1 = sell
    output wire [31:0] fire_px,
    output wire [31:0] fire_q,
    output wire [31:0] fire_id,
    output wire [31:0] fire_sof,
    output wire [31:0] fire_eom
);
    wire [31:0] spread = ask_px - bid_px;
    wire        both   = bid_v && ask_v && (spread <= cfg_max_spread);
    wire [47:0] bq     = {16'd0, bid_q};
    wire [47:0] aq     = {16'd0, ask_q};
    wire        buy_c  = both && (bq >= (aq << cfg_imb_shift));
    wire        sell_c = both && (aq >= (bq << cfg_imb_shift));
    reg         pb, ps;
    wire        buy_f  = upd && buy_c && !pb;
    wire        sell_f = upd && sell_c && !ps && !buy_f;
    wire [31:0] avail  = buy_f ? ask_q : bid_q;

    assign fire      = cfg_enable && (buy_f || sell_f);
    assign fire_side = !buy_f;
    assign fire_px   = buy_f ? ask_px : bid_px;
    assign fire_q    = (avail < cfg_max_qty) ? avail : cfg_max_qty;
    assign fire_id   = upd_id;
    assign fire_sof  = upd_sof;
    assign fire_eom  = upd_eom;

    always @(posedge clk) begin
        if (rst) begin
            pb <= 1'b0; ps <= 1'b0;
        end else if (upd) begin
            pb <= buy_c; ps <= sell_c;
        end
    end
endmodule
