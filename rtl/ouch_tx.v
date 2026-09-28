// ouch_tx.v -- OUCH 4.2 Enter Order ('O', 49 bytes) serializer onto a 64-bit
// stream (7 beats, last beat keep = 8'h01).  Orders that fire while a message
// is being sent wait in a small queue; back-to-back messages have no bubble.
// Latency is measured here: at the cycle the first beat is presented,
//   lat_pkt = cycle(first out beat) - cycle(first beat of triggering packet)
//   lat_msg = cycle(first out beat) - cycle(beat carrying last byte of msg)
//
// Byte layout (big endian integers):
//   0 'O' | 1..14 token | 15 side B/S | 16..19 shares | 20..27 stock |
//   28..31 price | 32..35 TIF=0 (IOC) | 36..39 firm | 40 display 'Y' |
//   41 capacity 'P' | 42 ISO 'N' | 43..46 min qty 0 | 47 cross 'N' |
//   48 customer type 'N'
// Token: "TT" + 12 upper-case hex digits of a 48-bit counter starting at 1.
module ouch_tx #(
    parameter QLOG2 = 3
) (
    input  wire        clk,
    input  wire        rst,
    input  wire [31:0] now,
    input  wire        fire,
    input  wire        fire_side,
    input  wire [31:0] fire_px,
    input  wire [31:0] fire_q,
    input  wire [31:0] fire_id,
    input  wire [31:0] fire_sof,
    input  wire [31:0] fire_eom,
    input  wire [63:0] cfg_stock,
    input  wire [31:0] cfg_firm,
    output reg         out_valid,
    output reg  [63:0] out_data,
    output reg  [7:0]  out_keep,
    output reg         out_last,
    output reg         lat_valid,
    output reg  [31:0] lat_pkt,
    output reg  [31:0] lat_msg,
    output reg  [31:0] lat_id,
    output reg  [31:0] lat_min,
    output reg  [31:0] lat_max,
    output reg  [31:0] cnt_orders,
    output reg  [31:0] cnt_qovf
);
    localparam QW = 1 + 32*5;
    wire [QW-1:0] frec = {fire_side, fire_px, fire_q, fire_id, fire_sof, fire_eom};
    wire          can_load = !out_valid || out_last;
    wire          q_empty, q_full, q_ovf;
    wire [QW-1:0] q_dout;
    wire [QLOG2:0] q_cnt;
    wire          load   = can_load && (!q_empty || fire);
    wire [QW-1:0] src    = q_empty ? frec : q_dout;
    wire          q_pop  = can_load && !q_empty;
    wire          q_push = fire && !(can_load && q_empty);

    sync_fifo #(.W(QW), .LOG2(QLOG2)) u_q (
        .clk(clk), .rst(rst), .push(q_push), .din(frec), .pop(q_pop),
        .dout(q_dout), .empty(q_empty), .full(q_full), .count(q_cnt), .ovf(q_ovf));

    wire        s_side = src[160];
    wire [31:0] s_px   = src[159:128];
    wire [31:0] s_q    = src[127:96];
    wire [31:0] s_id   = src[95:64];
    wire [31:0] s_sof  = src[63:32];
    wire [31:0] s_eom  = src[31:0];

    reg [47:0]  tok;
    reg [391:0] msg;
    reg [327:0] rest;
    reg [2:0]   beat;
    integer     i;

    function [7:0] hexc;
        input [3:0] n;
        hexc = (n < 4'd10) ? (8'h30 + {4'd0, n}) : (8'h37 + {4'd0, n});
    endfunction

    always @* begin
        msg = 392'd0;
        msg[8*0 +: 8] = "O";
        msg[8*1 +: 8] = "T";
        msg[8*2 +: 8] = "T";
        for (i = 0; i < 12; i = i + 1)
            msg[8*(3+i) +: 8] = hexc(tok[47-4*i -: 4]);
        msg[8*15 +: 8] = s_side ? "S" : "B";
        for (i = 0; i < 4; i = i + 1) begin
            msg[8*(16+i) +: 8] = s_q[31-8*i -: 8];
            msg[8*(28+i) +: 8] = s_px[31-8*i -: 8];
            msg[8*(32+i) +: 8] = 8'd0;                    // TIF 0 = IOC
            msg[8*(36+i) +: 8] = cfg_firm[31-8*i -: 8];
            msg[8*(43+i) +: 8] = 8'd0;                    // min qty
        end
        for (i = 0; i < 8; i = i + 1)
            msg[8*(20+i) +: 8] = cfg_stock[63-8*i -: 8];
        msg[8*40 +: 8] = "Y";
        msg[8*41 +: 8] = "P";
        msg[8*42 +: 8] = "N";
        msg[8*47 +: 8] = "N";
        msg[8*48 +: 8] = "N";
    end

    wire [31:0] lp = now + 32'd1 - s_sof;
    wire [31:0] lm = now + 32'd1 - s_eom;

    always @(posedge clk) begin
        if (rst) begin
            out_valid <= 1'b0; out_data <= 64'd0; out_keep <= 8'd0; out_last <= 1'b0;
            lat_valid <= 1'b0; lat_pkt <= 0; lat_msg <= 0; lat_id <= 0;
            lat_min <= 32'hffff_ffff; lat_max <= 32'd0;
            cnt_orders <= 0; cnt_qovf <= 0;
            tok <= 48'd1; rest <= 328'd0; beat <= 3'd0;
        end else begin
            cnt_qovf <= cnt_qovf + (q_ovf ? 32'd1 : 32'd0);
            if (load) begin
                out_valid <= 1'b1;
                out_data  <= msg[63:0];
                out_keep  <= 8'hff;
                out_last  <= 1'b0;
                rest      <= msg[391:64];
                beat      <= 3'd0;
                tok       <= tok + 48'd1;
                lat_valid <= 1'b1;
                lat_pkt   <= lp;
                lat_msg   <= lm;
                lat_id    <= s_id;
                if (lp < lat_min) lat_min <= lp;
                if (lp > lat_max) lat_max <= lp;
                cnt_orders <= cnt_orders + 32'd1;
            end else begin
                lat_valid <= 1'b0;
                if (out_valid && !out_last) begin
                    beat     <= beat + 3'd1;
                    out_data <= rest[63:0];
                    rest     <= rest >> 64;
                    if (beat == 3'd5) begin
                        out_last <= 1'b1;
                        out_keep <= 8'h01;
                    end
                end else if (out_valid && out_last) begin
                    out_valid <= 1'b0;
                    out_last  <= 1'b0;
                    out_keep  <= 8'd0;
                end
            end
        end
    end
endmodule
