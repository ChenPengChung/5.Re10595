#ifndef MODEL_FILE
#define MODEL_FILE

double HillFunction( const double Y )
{
    double Yb;
    double model = 0.0;

    if ( Y < 0.0 )
        { Yb = Y + LY; }
    else if ( Y > LY )
        { Yb = Y - LY; }
    else
        { Yb = Y; }
    
    //left
		if ( (double) Yb <= (54./28.)*(9./54.)  ){
			model= (double)1./28.*min(28.,28.+ 0.006775070969851*(double)Yb*28*(double)Yb*28 - 0.0021245277758000*(double)Yb*28*(double)Yb*28*(double)Yb*28); 
		}	
		if ( (double) Yb > (54./28.)*(9./54.) && (double) Yb <= (54./28.)*(14./54.) ){
			model= 1./28.*(25.07355893131 + 0.9754803562315*(double)Yb*28 - 0.1016116352781*(double)Yb*28*(double)Yb*28 + 0.001889794677828*(double)Yb*28*(double)Yb*28*(double)Yb*28 ); 
		}
		if ( (double) Yb > (54./28.)*(14./54.) && (double) Yb <= (54./28.)*(20./54.) ){
			model= 1./28.*(25.79601052357 + 0.8206693007457*(double)Yb*28 - 0.09055370274339*(double)Yb*28*(double)Yb*28 + 0.001626510569859*(double)Yb*28*(double)Yb*28*(double)Yb*28);
		}
		if ( (double) Yb > (54./28.)*(20./54.) && (double) Yb <= (54./28.)*(30./54.) ){
			model= 1./28.*(40.46435022819 - 1.379581654948*(double)Yb*28 + 0.019458845041284*(double)Yb*28*(double)Yb*28 - 0.0002070318932190*(double)Yb*28*(double)Yb*28*(double)Yb*28);
		}
		if ( (double) Yb > (54./28.)*(30./54.) && (double) Yb <= (54./28.)*(40./54.) ){
			model= 1./28.*(17.92461334664 + 0.8743920332081*(double)Yb*28 - 0.05567361123058*(double)Yb*28*(double)Yb*28 + 0.0006277731764683*(double)Yb*28*(double)Yb*28*(double)Yb*28);
		}
		if ( (double) Yb > (54./28.)*(40./54.) && (double) Yb <= (54./28.)*(54./54.) ){
			model= 1./28.*max(0., 56.39011190988 - 2.010520359035*(double)Yb*28 + 0.01644919857549*(double)Yb*28*(double)Yb*28 + 0.00002674976141766*(double)Yb*28*(double)Yb*28*(double)Yb*28 );
		}
	//right
		if ( (double) Yb < LY-(54./28.)*(40./54.) && (double) Yb >= LY-(54./28.)*(54./54.) ){
			model= 1./28.*max(0., 56.39011190988 - 2.010520359035*(double)(LY-Yb)*28 + 0.01644919857549*(double)(LY-Yb)*28*(double)(LY-Yb)*28 + 0.00002674976141766*(double)(LY-Yb)*28*(double)(LY-Yb)*28*(double)(LY-Yb)*28 );
		}	
		if ( (double) Yb < LY-(54./28.)*(30./54.) && (double) Yb >= LY-(54./28.)*(40./54.) ){
			model= 1./28.*(17.92461334664 + 0.8743920332081*(double)(LY-Yb)*28 - 0.05567361123058*(double)(LY-Yb)*28*(double)(LY-Yb)*28 + 0.0006277731764683*(double)(LY-Yb)*28*(double)(LY-Yb)*28*(double)(LY-Yb)*28);
		}
		if ( (double) Yb < LY-(54./28.)*(20./54.) && (double) Yb >= LY-(54./28.)*(30./54.) ){
			model= 1./28.*(40.46435022819 - 1.379581654948*(double)(LY-Yb)*28 + 0.019458845041284*(double)(LY-Yb)*28*(double)(LY-Yb)*28 - 0.0002070318932190*(double)(LY-Yb)*28*(double)(LY-Yb)*28*(double)(LY-Yb)*28);
		}	
		if ( (double) Yb < LY-(54./28.)*(14./54.) && (double) Yb >= LY-(54./28.)*(20./54.) ){
			model= 1./28.*(25.79601052357 + 0.8206693007457*(double)(LY-Yb)*28 - 0.09055370274339*(double)(LY-Yb)*28*(double)(LY-Yb)*28 + 0.001626510569859*(double)(LY-Yb)*28*(double)(LY-Yb)*28*(double)(LY-Yb)*28);
		}
		if ( (double) Yb < LY-(54./28.)*(9./54.) && (double) Yb >= LY-(54./28.)*(14./54.) ){
			model= 1./28.*(25.07355893131 + 0.9754803562315*(double)(LY-Yb)*28 - 0.1016116352781*(double)(LY-Yb)*28*(double)(LY-Yb)*28 + 0.001889794677828*(double)(LY-Yb)*28*(double)(LY-Yb)*28*(double)(LY-Yb)*28);
		}	
		if ( (double) Yb >= LY-(54./28.)*(9./54.) ){
			model= 1./28.*min(28.,28.+ 0.006775070969851*(double)(LY-Yb)*28*(double)(LY-Yb)*28 - 0.0021245277758000*(double)(LY-Yb)*28*(double)(LY-Yb)*28*(double)(LY-Yb)*28);
		}
	
	/* if( Yb <= 0.5 ){
		model = 0.5;
	} else if ( Yb > 0.5 && Yb <= 1.5 ) {
		model = 0.75 - 0.5*Yb;
	} else if ( Yb > 1.5 && Yb < 7.5 ) {
		model = 0.0;
	} else if ( Yb >= 7.5 && Yb <= 8.5 ) {
		model = 0.5*Yb - 3.75;
	} else {
		model = 0.5;
	} */

	return model;
}

double ChannelFunction( const double Y )
{
	return 0.0;
}

#endif