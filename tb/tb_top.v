// tb_top.v -- drives the pipeline from a stimulus file and dumps everything
// the Python checker compares bit-exactly against the golden model.
//
// Stimulus: one line per clock cycle, "valid last keep data" in hex.
// Line i is presented to the DUT during cycle i (cycle 0 = first cycle after
// reset), so Python can compute expected latencies from line indices alone.
//
// Output lines:
//   B <msg_id> <bid_v> <bid_px> <bid_q> <ask_v> <ask_px> <ask_q>  (hex vectors)
//   O <first_beat_cycle> <lat_pkt> <lat_msg> <msg_id> <49 bytes hex, byte48 first>
//   T <slot> <entry hex>          (valid order-table entries at end)
//   C <name> <value>              (counters at end)
`timescale 1ns/1ps
module tb;
    parameter ORDER_BITS = 12;
    parameter DEPTH      = 8;
    parameter FIFO_LOG2  = 3;
    parameter OQ_LOG2    = 3;
    parameter REF_HASH   = 1;

    reg clk = 1'b0;
    always #1 clk = ~clk;
    reg rst = 1'b1;

    reg  [63:0] in_data  = 64'd0;
    reg  [7:0]  in_keep  = 8'd0;
    reg         in_valid = 1'b0;
    reg         in_last  = 1'b0;
    reg  [15:0] cfg_locate;
    reg         cfg_enable;
    reg  [3:0]  cfg_imb_shift;
    reg  [31:0] cfg_max_spread, cfg_max_qty, cfg_firm;
    reg  [63:0] cfg_stock;

    wire        out_valid, out_last, lat_valid, busy;
    wire [63:0] out_data;
    wire [7:0]  out_keep, fifo_max;
    wire [31:0] lat_pkt, lat_msg, lat_id, lat_min, lat_max;
    wire        tob_bid_v, tob_ask_v;
    wire [31:0] tob_bid_px, tob_bid_q, tob_ask_px, tob_ask_q;
    wire [31:0] cnt_msgs, cnt_pkts, cnt_parse_err, cnt_rel, cnt_coll, cnt_miss;
    wire [31:0] cnt_evict, cnt_drop, cnt_ovf, cnt_orders, cnt_qovf;

    t2t_top #(.ORDER_BITS(ORDER_BITS), .DEPTH(DEPTH), .FIFO_LOG2(FIFO_LOG2),
              .OQ_LOG2(OQ_LOG2), .REF_HASH(REF_HASH)) dut (
        .clk(clk), .rst(rst), .in_data(in_data), .in_keep(in_keep),
        .in_valid(in_valid), .in_last(in_last), .cfg_locate(cfg_locate),
        .cfg_enable(cfg_enable), .cfg_imb_shift(cfg_imb_shift),
        .cfg_max_spread(cfg_max_spread), .cfg_max_qty(cfg_max_qty),
        .cfg_stock(cfg_stock), .cfg_firm(cfg_firm),
        .out_valid(out_valid), .out_data(out_data), .out_keep(out_keep),
        .out_last(out_last), .lat_valid(lat_valid), .lat_pkt(lat_pkt),
        .lat_msg(lat_msg), .lat_id(lat_id), .lat_min(lat_min), .lat_max(lat_max),
        .tob_bid_v(tob_bid_v), .tob_bid_px(tob_bid_px), .tob_bid_q(tob_bid_q),
        .tob_ask_v(tob_ask_v), .tob_ask_px(tob_ask_px), .tob_ask_q(tob_ask_q),
        .cnt_msgs(cnt_msgs), .cnt_pkts(cnt_pkts), .cnt_parse_err(cnt_parse_err),
        .cnt_rel(cnt_rel), .cnt_coll(cnt_coll), .cnt_miss(cnt_miss),
        .cnt_evict(cnt_evict), .cnt_drop(cnt_drop), .cnt_ovf(cnt_ovf),
        .fifo_max(fifo_max), .cnt_orders(cnt_orders), .cnt_qovf(cnt_qovf),
        .busy(busy));

    reg [31:0] tcyc;
    always @(posedge clk) tcyc <= rst ? 32'd0 : tcyc + 32'd1;

    integer fi, fo, r, i, drain, nlines, ob;
    reg [8*512-1:0] stim_name, out_name;
    reg [31:0] vv, ll, kk;
    reg [63:0] dd;
    reg [391:0] oacc;
    reg [31:0] o_cyc, o_lp, o_lm, o_id;

    // ---------------- monitors ----------------
    initial ob = 0;
    always @(posedge clk) begin
        if (!rst) begin
            if (dut.upd)
                $fwrite(fo, "B %0d %h %h %h %h %h %h\n", dut.upd_id,
                        dut.u_eng.bid_v, dut.u_eng.bid_px, dut.u_eng.bid_q,
                        dut.u_eng.ask_v, dut.u_eng.ask_px, dut.u_eng.ask_q);
            if (out_valid) begin
                if (ob == 0) begin
                    oacc  = 392'd0;
                    o_cyc = tcyc;
                    if (!lat_valid) begin
                        $display("ERROR: lat_valid not aligned with first beat");
                    end
                    o_lp = lat_pkt; o_lm = lat_msg; o_id = lat_id;
                end
                oacc = oacc | ({328'd0, out_data} << (64*ob));
                ob = ob + 1;
                if (out_last) begin
                    if (ob != 7 || out_keep != 8'h01)
                        $display("ERROR: bad OUCH framing beats=%0d keep=%h", ob, out_keep);
                    $fwrite(fo, "O %0d %0d %0d %0d %h\n", o_cyc, o_lp, o_lm, o_id, oacc);
                    ob = 0;
                end
            end
        end
    end

    // ---------------- stimulus ----------------
    initial begin
        if (!$value$plusargs("STIM=%s", stim_name)) stim_name = "stim.txt";
        if (!$value$plusargs("OUT=%s", out_name)) out_name = "rtl_out.txt";
        if (!$value$plusargs("LOCATE=%d", cfg_locate)) cfg_locate = 16'd1;
        if (!$value$plusargs("ENABLE=%d", cfg_enable)) cfg_enable = 1'b1;
        if (!$value$plusargs("SHIFT=%d", cfg_imb_shift)) cfg_imb_shift = 4'd1;
        if (!$value$plusargs("SPREAD=%d", cfg_max_spread)) cfg_max_spread = 32'd500;
        if (!$value$plusargs("MAXQTY=%d", cfg_max_qty)) cfg_max_qty = 32'd500;
        if (!$value$plusargs("STOCK=%h", cfg_stock)) cfg_stock = "AAPL    ";
        if (!$value$plusargs("FIRM=%h", cfg_firm)) cfg_firm = "TTRD";
        if (!$value$plusargs("DRAIN=%d", drain)) drain = 200;
        fi = $fopen(stim_name, "r");
        if (fi == 0) begin
            $display("ERROR: cannot open stimulus %0s", stim_name);
            $finish;
        end
        fo = $fopen(out_name, "w");
        nlines = 0;
        repeat (4) @(posedge clk);
        rst <= 1'b0;
        r = $fscanf(fi, "%h %h %h %h\n", vv, ll, kk, dd);
        while (r == 4) begin
            in_valid <= vv[0];
            in_last  <= ll[0];
            in_keep  <= kk[7:0];
            in_data  <= dd;
            nlines = nlines + 1;
            @(posedge clk);
            r = $fscanf(fi, "%h %h %h %h\n", vv, ll, kk, dd);
        end
        in_valid <= 1'b0; in_last <= 1'b0; in_keep <= 8'd0; in_data <= 64'd0;
        i = 0;
        while ((busy || i < 4) && i < drain) begin
            @(posedge clk);
            i = i + 1;
        end
        repeat (2) @(posedge clk);
        for (i = 0; i < (1 << ORDER_BITS); i = i + 1)
            if (dut.u_eng.u_tab.mem[i][129])
                $fwrite(fo, "T %0d %h\n", i, dut.u_eng.u_tab.mem[i]);
        $fwrite(fo, "C lines %0d\n", nlines);
        $fwrite(fo, "C cycles %0d\n", tcyc);
        $fwrite(fo, "C msgs %0d\n", cnt_msgs);
        $fwrite(fo, "C pkts %0d\n", cnt_pkts);
        $fwrite(fo, "C parse_err %0d\n", cnt_parse_err);
        $fwrite(fo, "C relevant %0d\n", cnt_rel);
        $fwrite(fo, "C collisions %0d\n", cnt_coll);
        $fwrite(fo, "C misses %0d\n", cnt_miss);
        $fwrite(fo, "C evictions %0d\n", cnt_evict);
        $fwrite(fo, "C drops %0d\n", cnt_drop);
        $fwrite(fo, "C fifo_ovf %0d\n", cnt_ovf);
        $fwrite(fo, "C fifo_max %0d\n", fifo_max);
        $fwrite(fo, "C orders %0d\n", cnt_orders);
        $fwrite(fo, "C order_q_ovf %0d\n", cnt_qovf);
        $fwrite(fo, "C lat_min %0d\n", lat_min);
        $fwrite(fo, "C lat_max %0d\n", lat_max);
        $fwrite(fo, "C busy_at_end %0d\n", busy);
        $fwrite(fo, "C tob %0d %0d %0d %0d %0d %0d\n", tob_bid_v, tob_bid_px, tob_bid_q,
                tob_ask_v, tob_ask_px, tob_ask_q);
        $fclose(fo);
        $fclose(fi);
        $finish;
    end
endmodule
