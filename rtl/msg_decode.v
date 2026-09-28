// msg_decode.v -- combinational field extraction + filter.
// Passes only order-book messages (A F E C X D U) of the configured stock
// locate whose length matches the ITCH 5.0 spec.  All fields big endian.
module msg_decode #(
    parameter CAP = 40
) (
    input  wire             msg_valid,
    input  wire [8*CAP-1:0] msg_data,
    input  wire [15:0]      msg_len,
    input  wire [15:0]      cfg_locate,
    output wire             rec_valid,
    output wire [2:0]       rec_kind,   // 1 add, 2 reduce (E/C/X), 3 delete, 4 replace
    output wire             rec_side,   // 0 buy / bid, 1 sell / ask (add only)
    output wire [63:0]      rec_ref,
    output wire [63:0]      rec_ref2,   // replace: new order reference
    output wire [31:0]      rec_shares,
    output wire [31:0]      rec_price
);
`define MB(j) msg_data[8*(j) +: 8]
    wire [7:0]  t   = `MB(0);
    wire [15:0] loc = {`MB(1), `MB(2)};

    wire is_a = (t == "A") && (msg_len == 16'd36);
    wire is_f = (t == "F") && (msg_len == 16'd40);
    wire is_e = (t == "E") && (msg_len == 16'd31);
    wire is_c = (t == "C") && (msg_len == 16'd36);
    wire is_x = (t == "X") && (msg_len == 16'd23);
    wire is_d = (t == "D") && (msg_len == 16'd19);
    wire is_u = (t == "U") && (msg_len == 16'd35);
    wire is_add = is_a | is_f;
    wire is_red = is_e | is_c | is_x;

    wire [63:0] ref1  = {`MB(11), `MB(12), `MB(13), `MB(14), `MB(15), `MB(16), `MB(17), `MB(18)};
    wire [31:0] sh_a  = {`MB(20), `MB(21), `MB(22), `MB(23)};
    wire [31:0] px_a  = {`MB(32), `MB(33), `MB(34), `MB(35)};
    wire [31:0] sh_e  = {`MB(19), `MB(20), `MB(21), `MB(22)};
    wire [63:0] ref_u = {`MB(19), `MB(20), `MB(21), `MB(22), `MB(23), `MB(24), `MB(25), `MB(26)};
    wire [31:0] sh_u  = {`MB(27), `MB(28), `MB(29), `MB(30)};
    wire [31:0] px_u  = {`MB(31), `MB(32), `MB(33), `MB(34)};

    assign rec_valid  = msg_valid && (loc == cfg_locate) && (is_add | is_red | is_d | is_u);
    assign rec_kind   = is_add ? 3'd1 : is_red ? 3'd2 : is_d ? 3'd3 : 3'd4;
    assign rec_side   = (`MB(19) == "S");
    assign rec_ref    = ref1;
    assign rec_ref2   = ref_u;
    assign rec_shares = is_add ? sh_a : is_red ? sh_e : sh_u;
    assign rec_price  = is_add ? px_a : px_u;
`undef MB
endmodule
