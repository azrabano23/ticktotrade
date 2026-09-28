// tb_parser.v -- unit testbench for itch_parser alone.
// Output: "P <id> <len> <sof> <eom> <40 captured bytes hex, byte39 first>"
// per extracted message, then "C <name> <value>" counters.
`timescale 1ns/1ps
module tb_parser;
    localparam CAP = 40;
    reg clk = 1'b0;
    always #1 clk = ~clk;
    reg rst = 1'b1;
    reg [63:0] in_data = 64'd0;
    reg [7:0]  in_keep = 8'd0;
    reg        in_valid = 1'b0, in_last = 1'b0;
    reg [31:0] cyc;
    always @(posedge clk) cyc <= rst ? 32'd0 : cyc + 32'd1;

    wire             m_valid;
    wire [8*CAP-1:0] m_data;
    wire [15:0]      m_len;
    wire [31:0]      m_id, m_sof, m_eom, c_msgs, c_pkts, c_err;
    itch_parser #(.CAP(CAP)) dut (
        .clk(clk), .rst(rst), .in_data(in_data), .in_keep(in_keep),
        .in_valid(in_valid), .in_last(in_last), .now(cyc),
        .msg_valid(m_valid), .msg_data(m_data), .msg_len(m_len), .msg_id(m_id),
        .msg_sof(m_sof), .msg_eom(m_eom), .cnt_msgs(c_msgs), .cnt_pkts(c_pkts),
        .cnt_err(c_err));

    integer fi, fo, r;
    reg [8*512-1:0] stim_name, out_name;
    reg [31:0] vv, ll, kk;
    reg [63:0] dd;

    always @(posedge clk)
        if (!rst && m_valid)
            $fwrite(fo, "P %0d %0d %0d %0d %h\n", m_id, m_len, m_sof, m_eom, m_data);

    initial begin
        if (!$value$plusargs("STIM=%s", stim_name)) stim_name = "stim.txt";
        if (!$value$plusargs("OUT=%s", out_name)) out_name = "parser_out.txt";
        fi = $fopen(stim_name, "r");
        fo = $fopen(out_name, "w");
        repeat (4) @(posedge clk);
        rst <= 1'b0;
        r = $fscanf(fi, "%h %h %h %h\n", vv, ll, kk, dd);
        while (r == 4) begin
            in_valid <= vv[0]; in_last <= ll[0]; in_keep <= kk[7:0]; in_data <= dd;
            @(posedge clk);
            r = $fscanf(fi, "%h %h %h %h\n", vv, ll, kk, dd);
        end
        in_valid <= 1'b0; in_last <= 1'b0;
        repeat (4) @(posedge clk);
        $fwrite(fo, "C msgs %0d\nC pkts %0d\nC parse_err %0d\n", c_msgs, c_pkts, c_err);
        $fclose(fo);
        $finish;
    end
endmodule
