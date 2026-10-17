"use client";
import { createContext, useContext } from "react";

// True while a team member who is not a team admin is signed in, so client
// navigation leaves out the pages Brain only serves to team admins.
const RestrictedMember = createContext(false);
export const RestrictedMemberProvider = RestrictedMember.Provider;
export const useRestrictedMember = () => useContext(RestrictedMember);
