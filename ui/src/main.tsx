import { init, registerRoutes } from "@neutron-build/core/client";
import { routes } from "virtual:neutron/routes";
import "./styles/global.css";

registerRoutes(routes);
void init();
